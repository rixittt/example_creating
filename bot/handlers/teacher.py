from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.db import Database
from bot.handlers.common import (
    build_unique_candidate,
    extract_formula_from_task_text,
    get_teacher_from_callback,
    get_teacher_or_notify,
    send_pool_list,
    send_pool_task,
    show_generated_candidate,
)
from bot.handlers.states import TeacherCreateFlow
from bot.keyboards.common import teacher_menu_keyboard
from bot.keyboards.inline import generated_regen_keyboard, modes_keyboard, topics_keyboard
from bot.services.formula_renderer import FormulaRenderer
from bot.services.gemini_client import GeminiClient

router = Router()


@router.message(F.text == "Сгенерировать задания")
async def teacher_start_generation(message: Message, state: FSMContext, db: Database, llm: GeminiClient) -> None:
    teacher = await get_teacher_or_notify(message, db)
    if not teacher:
        return
    if not llm.enabled:
        await message.answer("LLM не настроена. Заполните GEMINI_API_KEY в .env")
        return

    topics = await db.list_topics()
    if not topics:
        await message.answer("В базе нет тем.")
        return

    await state.set_state(TeacherCreateFlow.waiting_topic)
    await state.update_data(teacher_id=teacher.id)
    await message.answer("Выберите тему:", reply_markup=topics_keyboard(topics))


@router.callback_query(TeacherCreateFlow.waiting_topic, F.data.startswith("teacher_topic:"))
async def teacher_select_topic(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.data or not callback.message:
        return

    topic_id = int(callback.data.split(":", 1)[1])
    topics = await db.list_topics()
    topic = next((item for item in topics if item.id == topic_id), None)
    if not topic:
        await callback.answer("Тема не найдена", show_alert=True)
        return

    await state.set_state(TeacherCreateFlow.waiting_mode)
    await state.update_data(topic_id=topic.id, topic_title=topic.title, topic_prompt=topic.llm_prompt)
    await callback.message.answer("Выберите режим задач:", reply_markup=modes_keyboard())
    await callback.answer()


@router.callback_query(TeacherCreateFlow.waiting_mode, F.data.startswith("teacher_mode:"))
async def teacher_select_mode(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data or not callback.message:
        return

    mode = callback.data.split(":", 1)[1]
    if mode not in {"learning", "testing"}:
        await callback.answer("Некорректный режим", show_alert=True)
        return

    await state.set_state(TeacherCreateFlow.waiting_count)
    await state.update_data(mode=mode)
    await callback.message.answer("Введите количество заданий (1-10):")
    await callback.answer()


@router.message(TeacherCreateFlow.waiting_count)
async def teacher_set_count(
    message: Message,
    state: FSMContext,
    llm: GeminiClient,
    renderer: FormulaRenderer,
    db: Database,
) -> None:
    value = (message.text or "").strip()
    if not value.isdigit():
        await message.answer("Нужно ввести число от 1 до 10.")
        return

    count = int(value)
    if count < 1 or count > 10:
        await message.answer("Количество должно быть от 1 до 10.")
        return

    data = await state.get_data()
    topic_title = str(data["topic_title"])
    topic_prompt = str(data.get("topic_prompt") or topic_title)
    mode = str(data["mode"])
    teacher_id = int(data["teacher_id"])
    topic_id = int(data["topic_id"])

    recent_topic_task_texts = await db.list_recent_teacher_formulas(teacher_id, topic_id, mode, limit=100)
    recent_other_topics_texts = await db.list_recent_teacher_formulas_other_topics(teacher_id, topic_id, mode, limit=30)

    same_topic_formulas = [
        extract_formula_from_task_text(item)
        for item in recent_topic_task_texts[:30]
        if extract_formula_from_task_text(item)
    ]
    cross_topic_formulas = [
        extract_formula_from_task_text(item)
        for item in recent_other_topics_texts[:30]
        if extract_formula_from_task_text(item)
    ]
    forbidden_formulas = same_topic_formulas + cross_topic_formulas

    await message.answer("Генерирую варианты, это может занять до минуты…")
    candidates: list[dict[str, str | bytes | None]] = []
    generated_formulas: list[str] = []
    for i in range(count):
        try:
            candidate = await build_unique_candidate(
                llm,
                renderer,
                db,
                teacher_id,
                topic_prompt,
                topic_title,
                mode,
                i + 1,
                forbidden_formulas=forbidden_formulas + generated_formulas,
                existing_formulas=generated_formulas,
            )
            candidates.append(candidate)
            generated_formula = str(candidate.get("latex") or "").strip()
            if generated_formula:
                generated_formulas.append(generated_formula)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"Ошибка генерации кандидата #{i + 1}: {exc}")
            break

    if not candidates:
        await state.clear()
        await message.answer("Не удалось сгенерировать задания.", reply_markup=teacher_menu_keyboard())
        return

    await state.set_state(TeacherCreateFlow.reviewing_generated)
    await state.update_data(
        total_to_generate=len(candidates),
        generated_index=0,
        generated_candidates=candidates,
        forbidden_formulas=forbidden_formulas,
    )
    await show_generated_candidate(message, state)


@router.callback_query(TeacherCreateFlow.reviewing_generated, F.data == "teacher_gen:regenerate")
async def teacher_regenerate(
    callback: CallbackQuery,
    state: FSMContext,
    llm: GeminiClient,
    renderer: FormulaRenderer,
    db: Database,
) -> None:
    if not callback.message:
        return

    data = await state.get_data()
    topic_title = str(data["topic_title"])
    topic_prompt = str(data.get("topic_prompt") or topic_title)
    mode = str(data["mode"])
    teacher_id = int(data["teacher_id"])
    generated_index = int(data.get("generated_index", 0))
    candidates = list(data.get("generated_candidates", []))

    if generated_index >= len(candidates):
        await callback.answer("Нет кандидата для перегенерации", show_alert=True)
        return

    forbidden_formulas = list(data.get("forbidden_formulas", []))
    already_generated = [
        str(item.get("latex") or "").strip()
        for idx, item in enumerate(candidates)
        if idx != generated_index and str(item.get("latex") or "").strip()
    ]

    try:
        candidates[generated_index] = await build_unique_candidate(
            llm,
            renderer,
            db,
            teacher_id,
            topic_prompt,
            topic_title,
            mode,
            generated_index + 1,
            forbidden_formulas=forbidden_formulas + already_generated,
            existing_formulas=already_generated,
        )
    except Exception as exc:  # noqa: BLE001
        await callback.answer(f"Ошибка генерации: {exc}", show_alert=True)
        return

    await state.update_data(generated_candidates=candidates)
    await show_generated_candidate(callback.message, state)
    await callback.answer()


@router.callback_query(TeacherCreateFlow.reviewing_generated, F.data == "teacher_gen:approve")
async def teacher_approve(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.message:
        return

    data = await state.get_data()
    teacher_id = int(data["teacher_id"])
    topic_id = int(data["topic_id"])
    mode = str(data["mode"])
    generated_index = int(data.get("generated_index", 0))
    total_to_generate = int(data.get("total_to_generate", 1))

    candidate_text = str(data.get("candidate_text", ""))
    candidate_hint = data.get("candidate_hint")
    candidate_image_file_id = data.get("candidate_image_file_id")
    candidate_answer = data.get("candidate_answer")

    task_id = await db.create_task(
        topic_id=topic_id,
        teacher_id=teacher_id,
        mode=mode,
        task_text=candidate_text,
        task_hint_text=candidate_hint,
        task_answer_text=candidate_answer,
        task_image_file_id=candidate_image_file_id,
    )

    if task_id is None:
        await callback.message.answer(
            "Такой пример уже есть в вашей базе. Пожалуйста, перегенерируйте его.",
            reply_markup=generated_regen_keyboard(),
        )
        await callback.answer()
        return

    generated_index += 1
    if generated_index >= total_to_generate:
        await state.clear()
        await callback.message.answer(
            f"Готово! Добавлено задач: {generated_index}. Последняя задача ID: {task_id}",
            reply_markup=teacher_menu_keyboard(),
        )
        await callback.answer()
        return

    await state.update_data(generated_index=generated_index)
    await callback.message.answer(f"Задача #{generated_index} подтверждена (ID {task_id}).")
    await show_generated_candidate(callback.message, state)
    await callback.answer()


@router.callback_query(TeacherCreateFlow.reviewing_generated, F.data == "teacher_gen:skip")
async def teacher_skip_candidate(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return

    data = await state.get_data()
    generated_index = int(data.get("generated_index", 0))
    total_to_generate = int(data.get("total_to_generate", 1))

    generated_index += 1
    if generated_index >= total_to_generate:
        await state.clear()
        await callback.message.answer(
            "Готово! Генерация завершена. Пропущенный кандидат не добавлен в пул.",
            reply_markup=teacher_menu_keyboard(),
        )
        await callback.answer()
        return

    await state.update_data(generated_index=generated_index)
    await show_generated_candidate(callback.message, state)
    await callback.answer()


@router.callback_query(TeacherCreateFlow.reviewing_generated, F.data == "teacher_gen:cancel")
async def teacher_cancel_generation(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message:
        await callback.message.answer("Генерация отменена.", reply_markup=teacher_menu_keyboard())
    await state.clear()
    await callback.answer()


@router.message(F.text == "Мой пул заданий")
async def teacher_pool(message: Message, state: FSMContext, db: Database) -> None:
    teacher = await get_teacher_or_notify(message, db)
    if not teacher:
        return

    tasks = await db.list_teacher_tasks(teacher.id)
    if not tasks:
        await message.answer("Пул задач пока пуст.")
        return

    await state.update_data(teacher_pool_ids=[task.id for task in tasks], teacher_pool_list_page=0)
    await send_pool_list(message, tasks, page=0)


@router.callback_query(F.data.startswith("pool_list_nav:"))
async def teacher_pool_list_nav(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.data or not callback.message:
        return

    direction = callback.data.split(":", 1)[1]
    teacher = await get_teacher_from_callback(callback, db)
    if not teacher:
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    tasks = await db.list_teacher_tasks(teacher.id)
    if not tasks:
        await callback.answer("Пул пуст", show_alert=True)
        return

    data = await state.get_data()
    page = int(data.get("teacher_pool_list_page", 0))
    total_pages = max((len(tasks) - 1) // 10 + 1, 1)

    if direction == "next":
        page = min(page + 1, total_pages - 1)
    else:
        page = max(page - 1, 0)

    await state.update_data(teacher_pool_list_page=page)
    await send_pool_list(callback.message, tasks, page=page, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("pool_open:"))
async def teacher_pool_open(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.data or not callback.message:
        return

    task_id = int(callback.data.split(":", 1)[1])
    teacher = await get_teacher_from_callback(callback, db)
    if not teacher:
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    tasks = await db.list_teacher_tasks(teacher.id)
    ids = [task.id for task in tasks]
    if task_id not in ids:
        await callback.answer("Задача не найдена", show_alert=True)
        return

    index = ids.index(task_id)
    await state.update_data(teacher_pool_ids=ids)
    await state.update_data(teacher_pool_current_id=task_id)
    await send_pool_task(callback.message, tasks[index], index, len(tasks))
    await callback.answer()


@router.callback_query(F.data.startswith("pool_nav:"))
async def teacher_pool_nav(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.data or not callback.message:
        return

    direction = callback.data.split(":", 1)[1]
    teacher = await get_teacher_from_callback(callback, db)
    if not teacher:
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    tasks = await db.list_teacher_tasks(teacher.id)
    if not tasks:
        await callback.answer("Пул пуст", show_alert=True)
        return

    data = await state.get_data()
    ids = data.get("teacher_pool_ids") or [task.id for task in tasks]
    current_id = int(data.get("teacher_pool_current_id", ids[0]))
    index = ids.index(current_id) if current_id in ids else 0

    if direction == "next":
        index = min(index + 1, len(ids) - 1)
    else:
        index = max(index - 1, 0)

    next_id = ids[index]
    task = next(item for item in tasks if item.id == next_id)
    await state.update_data(teacher_pool_current_id=next_id)
    await send_pool_task(callback.message, task, index, len(ids))
    await callback.answer()


@router.callback_query(F.data == "pool_noop")
async def teacher_pool_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "pool_back")
async def teacher_pool_back(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.message:
        return
    teacher = await get_teacher_from_callback(callback, db)
    if not teacher:
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    tasks = await db.list_teacher_tasks(teacher.id)
    data = await state.get_data()
    page = int(data.get("teacher_pool_list_page", 0))
    await send_pool_list(callback.message, tasks, page=page)
    await callback.answer()
