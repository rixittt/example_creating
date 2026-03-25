import base64
import io
import logging
import re

from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest

from bot.db import Database, Student, Task, Teacher, TheoryPage, Topic
from bot.keyboards.common import (
    learning_after_answer_keyboard,
    learning_incorrect_keyboard,
    student_menu_keyboard,
    teacher_menu_keyboard,
    theory_keyboard,
    waiting_answer_keyboard,
)
from bot.keyboards.inline import (
    generated_review_keyboard,
    learning_answer_keyboard,
    pool_list_keyboard,
    pool_nav_keyboard,
)
from bot.services.formula_renderer import FormulaRenderer
from bot.services.gemini_client import DEFAULT_ANSWER_CHECK_PROMPT_TEMPLATE, GeminiClient
from bot.handlers.states import StudentFlow

logger = logging.getLogger(__name__)

DEFAULT_GENERATION_BASE_PROMPT_TEMPLATE = (
    "{{topic_prompt}}\n\n"
    "Важно: сгенерированный пример должен строго относиться к теме «{{topic_title}}». "
    "Не используй методы из других тем и не смешивай темы в одном примере.\n"
    "Важно: пример должен решаться кратко — примерно за 3-5 ключевых шагов, без громоздких преобразований.\n"
    "Важно для поля 'подсказка': это должен быть первый практический шаг решения "
    "(с чего начать), конкретно для этого примера, а не абстрактный совет.\n"
    "Подсказка должна быть написана простым русским текстом; LaTeX используй только для коротких фрагментов формул.\n"
    "Избегай тривиальных примеров и старайся давать нетривиальный, но решаемый уровень."
)

DEFAULT_GENERATION_FORBIDDEN_SUFFIX_TEMPLATE = (
    "Важно: не повторяй следующие {{forbidden_count}} последних формул "
    "(в том числе в эквивалентной форме):\n"
    "{{forbidden_lines}}\n"
    "Сгенерируй новый, отличный пример."
)


def render_prompt_template(template: str, variables: dict[str, str]) -> str:
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


async def build_candidate(
    llm: GeminiClient,
    renderer: FormulaRenderer,
    db: Database,
    topic_prompt: str,
    topic_title: str,
    mode: str,
    index: int,
    forbidden_formulas: list[str] | None = None,
) -> dict[str, str | bytes | None]:
    base_template = await db.get_prompt_template(
        "generation_base",
        DEFAULT_GENERATION_BASE_PROMPT_TEMPLATE,
    )
    base_prompt = render_prompt_template(
        base_template,
        {
            "topic_prompt": topic_prompt,
            "topic_title": topic_title,
        },
    )

    effective_prompt = base_prompt
    if forbidden_formulas:
        unique_forbidden_formulas = list(dict.fromkeys(formula for formula in forbidden_formulas if formula.strip()))
        forbidden_lines = "\n".join(f"- {formula}" for formula in unique_forbidden_formulas)
        forbidden_suffix_template = await db.get_prompt_template(
            "generation_forbidden_suffix",
            DEFAULT_GENERATION_FORBIDDEN_SUFFIX_TEMPLATE,
        )
        forbidden_suffix = render_prompt_template(
            forbidden_suffix_template,
            {
                "forbidden_count": str(len(unique_forbidden_formulas)),
                "forbidden_lines": forbidden_lines,
            },
        )
        effective_prompt = (
            f"{base_prompt}\n\n"
            f"{forbidden_suffix}"
        )

    generated = await llm.generate_task(effective_prompt)
    image_bytes = renderer.render_integral_image(generated.latex_integral)
    text = f"Вычислите интеграл: {generated.latex_integral}"
    hint = clean_student_text(generated.hint) if mode == "learning" else None
    answer = clean_student_text(generated.answer)
    return {
        "text": text,
        "hint": hint,
        "answer": answer,
        "image_bytes": image_bytes,
        "latex": generated.latex_integral,
        "index": str(index),
    }


async def build_unique_candidate(
    llm: GeminiClient,
    renderer: FormulaRenderer,
    db: Database,
    teacher_id: int,
    topic_prompt: str,
    topic_title: str,
    mode: str,
    index: int,
    forbidden_formulas: list[str] | None = None,
    existing_formulas: list[str] | None = None,
    max_attempts: int = 7,
) -> dict[str, str | bytes | None]:
    existing_normalized = {normalize_formula(formula) for formula in (existing_formulas or []) if formula.strip()}

    for _ in range(max_attempts):
        candidate = await build_candidate(
            llm,
            renderer,
            db,
            topic_prompt,
            topic_title,
            mode,
            index,
            forbidden_formulas=forbidden_formulas,
        )
        candidate_formula = str(candidate.get("latex") or "").strip()
        normalized_formula = normalize_formula(candidate_formula)
        if not normalized_formula or normalized_formula in existing_normalized:
            continue

        exists_in_teacher_pool = await db.has_teacher_formula(teacher_id, candidate_formula)
        if exists_in_teacher_pool:
            continue

        return candidate

    raise RuntimeError(
        "Не удалось сгенерировать уникальный пример после нескольких попыток. "
        "Попробуйте уменьшить количество за раз или перегенерировать."
    )


async def show_generated_candidate(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    topic_title = str(data["topic_title"])
    mode = str(data["mode"])
    generated_index = int(data.get("generated_index", 0))
    total_to_generate = int(data["total_to_generate"])

    candidates = list(data.get("generated_candidates", []))
    if generated_index >= len(candidates):
        await message.answer("Кандидатов больше нет.", reply_markup=teacher_menu_keyboard())
        await state.clear()
        return

    candidate = candidates[generated_index]
    img = BufferedInputFile(candidate["image_bytes"], filename=f"candidate_{generated_index + 1}.png")

    text = (
        f"Задание #{generated_index + 1}\n"
        f"Тема: {topic_title}\n"
        f"Режим: {'Обучение' if mode == 'learning' else 'Тестирование'}"
    )
    if mode == "learning":
        hint = clean_student_text(str(candidate.get("hint") or ""))
        answer = clean_student_text(str(candidate.get("answer") or ""))
        if hint:
            text += f"\nПодсказка: {hint}"
        if answer:
            text += f"\nОтвет: {answer}"
    sent = await message.answer_photo(img, caption=text, reply_markup=generated_review_keyboard())

    file_id = sent.photo[-1].file_id if sent.photo else None
    await state.update_data(
        candidate_text=candidate["text"],
        candidate_hint=candidate["hint"],
        candidate_answer=candidate["answer"],
        candidate_image_file_id=file_id,
    )


async def send_pool_list(message: Message, tasks: list[Task], page: int, edit: bool = False) -> None:
    if edit:
        try:
            await message.edit_text("Ваш пул заданий:", reply_markup=pool_list_keyboard(tasks, page=page))
            return
        except TelegramBadRequest:
            pass
    await message.answer("Ваш пул заданий:", reply_markup=pool_list_keyboard(tasks, page=page))


async def send_pool_task(message: Message, task: Task, index: int, total: int) -> None:
    text = (
        f"Задание #{task.id}\n"
        f"Тема: {task.topic_title}\n"
        f"Режим: {'Обучение' if task.mode == 'learning' else 'Тестирование'}"
    )

    if not task.task_image_file_id:
        text += f"\n{task.task_text}"
    if task.task_image_file_id:
        await message.answer_photo(task.task_image_file_id, caption=text, reply_markup=pool_nav_keyboard(index, total))
    else:
        await message.answer(text, reply_markup=pool_nav_keyboard(index, total))


async def send_learning_task(message: Message, state: FSMContext, db: Database, topic_id: int | None = None, **_: object) -> None:
    student = await get_student_or_notify(message, db)
    if not student:
        return

    if topic_id is None:
        state_data = await state.get_data()
        topic_id = int(state_data.get("selected_topic_id", 0)) or None
    if topic_id is None:
        await message.answer("Сначала выберите тему.")
        return

    task = await db.get_next_task(student.id, student.teacher_id, "learning", topic_id=topic_id)
    if not task:
        await state.clear()
        await message.answer("Доступные задания для обучения закончились.", reply_markup=student_menu_keyboard())
        return

    await state.set_state(StudentFlow.waiting_learning_answer)
    await state.update_data(
        task_id=task.id,
        current_hint=task.task_hint_text,
        current_answer=task.task_answer_text,
        current_task_text=task.task_text,
        learning_fail_attempts=0,
        selected_topic_id=topic_id,
    )
    await send_task_with_prompt(message, task)


async def send_testing_task(
    message: Message,
    state: FSMContext,
    db: Database,
    student: Student,
    topic_id: int | None = None,
    **_: object,
) -> None:
    if topic_id is None:
        state_data = await state.get_data()
        topic_id = int(state_data.get("selected_topic_id", 0)) or None
    if topic_id is None:
        await message.answer("Сначала выберите тему.")
        return

    task = await db.get_next_task(student.id, student.teacher_id, "testing", topic_id=topic_id)
    if not task:
        await remove_inline_keyboard(message)
        solved_count = await db.count_student_answers_by_mode_and_topic(student.id, "testing", topic_id)
        total_topic_tasks = await db.count_tasks_by_teacher_mode_topic(student.teacher_id, "testing", topic_id)
        total_target = min(10, total_topic_tasks)
        await state.clear()

        if total_target > 0:
            await message.answer(
                f"Тестирование завершено: {solved_count} из {total_target} задач.",
                reply_markup=student_menu_keyboard(),
            )
        else:
            await message.answer(
                "Для этой темы пока нет заданий для тестирования.",
                reply_markup=student_menu_keyboard(),
            )
        return

    solved_count = await db.count_student_answers_by_mode_and_topic(student.id, "testing", topic_id)
    total_topic_tasks = await db.count_tasks_by_teacher_mode_topic(student.teacher_id, "testing", topic_id)
    total_target = min(10, total_topic_tasks)
    current_index = min(solved_count + 1, max(total_target, 1))

    await state.set_state(StudentFlow.waiting_testing_answer)
    await state.update_data(
        task_id=task.id,
        current_answer=task.task_answer_text,
        current_task_text=task.task_text,
        selected_topic_id=topic_id,
        testing_progress_current=current_index,
        testing_progress_total=total_target,
    )
    progress_text = f"Задача {current_index} из {total_target}" if total_target > 0 else None
    await send_task_with_prompt(message, task, progress_text=progress_text)


async def send_theory_page(message: Message, pages: list[TheoryPage], index: int) -> None:
    page = pages[index]
    text = f"{page.title}\n\n{page.text_content}"
    has_next = index < len(pages) - 1

    if page.image_file_id:
        await message.answer_photo(page.image_file_id, caption=text, reply_markup=theory_keyboard(has_next))
        return

    await message.answer(text, reply_markup=theory_keyboard(has_next))


async def remove_inline_keyboard(message: Message) -> None:
    if not message.reply_markup:
        return

    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


def format_student_display_name(student: Student) -> str:
    return f"{student.name}_{student.group_number}_{student.student_number}"


async def send_task_with_prompt(message: Message, task: Task, progress_text: str | None = None) -> None:
    lines = [f"Тема: {task.topic_title}", f"Задание #{task.id}:"]

    if progress_text:
        lines.append(progress_text)

    if not task.task_image_file_id:
        lines.append(task.task_text)

    answer_button = waiting_answer_keyboard() if task.mode == "testing" else None
    if task.mode == "learning" and task.task_answer_text:
        lines.append("Чтобы посмотреть решение-ответ, нажмите кнопку ниже.")
        answer_button = learning_answer_keyboard()

    lines.append("Пришлите фото ответа.")
    text = "\n".join(lines)

    if task.task_image_file_id:
        await message.answer_photo(task.task_image_file_id, caption=text, reply_markup=answer_button)
    else:
        await message.answer(text, reply_markup=answer_button)

    if task.mode == "learning":
        await message.answer("Отправьте фото с ответом или нажмите «Пропустить задание».", reply_markup=waiting_answer_keyboard())


async def send_learning_answer_photo(
    message: Message,
    answer_text: str,
    renderer: FormulaRenderer,
    caption: str = "Правильный ответ",
) -> None:
    prepared_answer = prepare_latex_for_render(answer_text)
    try:
        image_bytes = renderer.render_integral_image(prepared_answer)
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"Не удалось отрисовать ответ: {exc}")
        await message.answer(f"Ответ (текстом): {prepared_answer}")
        return

    image = BufferedInputFile(image_bytes, filename="learning_answer.png")
    await message.answer_photo(image, caption=caption)


async def process_learning_attempt(
    message: Message,
    state: FSMContext,
    db: Database,
    llm: GeminiClient,
    renderer: FormulaRenderer,
    is_retry: bool,
) -> None:
    student = await get_student_or_notify(message, db)
    if not student:
        return

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
        await message.answer("Не удалось найти эталонный ответ для проверки. Попробуйте следующее задание.")
        return

    progress_message = await message.answer("⏳ Проверяю фото ответа…")
    current_task_text = str(state_data.get("current_task_text") or f"Задание #{task_id}")
    check = await check_student_answer(message, llm, db, file_id, expected_answer, current_task_text)
    await finish_progress_message(progress_message, check)
    if check is None:
        return

    if check.verdict == "unreadable":
        fail_attempts = int(state_data.get("learning_fail_attempts", 0)) + 1
        await state.update_data(learning_fail_attempts=fail_attempts)
        if fail_attempts >= 3:
            await db.save_answer(student.id, task_id, "learning", answer_image_file_id=None, is_correct=False, is_skipped=True)
            answer_text = str(state_data.get("current_answer") or "").strip()
            await message.answer("Три неудачные попытки.")
            if answer_text:
                await send_learning_answer_photo(message, answer_text, renderer, caption="Правильный ответ")
            else:
                await message.answer("Правильный ответ недоступен.")
            selected_topic_id = int(state_data.get("selected_topic_id", 0)) or None
            await send_learning_task(message, state, db, topic_id=selected_topic_id)
            return

        await state.set_state(StudentFlow.waiting_learning_retry_answer if is_retry else StudentFlow.waiting_learning_answer)
        await message.answer(
            "Не удалось распознать ответ. Сфотографируйте и отправьте фото ответа еще раз.",
            reply_markup=waiting_answer_keyboard(),
        )
        return

    if check.verdict == "incorrect":
        fail_attempts = int(state_data.get("learning_fail_attempts", 0)) + 1
        await state.update_data(learning_fail_attempts=fail_attempts)
        if fail_attempts >= 3:
            await db.save_answer(student.id, task_id, "learning", answer_image_file_id=None, is_correct=False, is_skipped=True)
            answer_text = str(state_data.get("current_answer") or "").strip()
            await message.answer("Три неверные попытки.")
            if answer_text:
                await send_learning_answer_photo(message, answer_text, renderer, caption="Правильный ответ")
            else:
                await message.answer("Правильный ответ недоступен.")
            selected_topic_id = int(state_data.get("selected_topic_id", 0)) or None
            await send_learning_task(message, state, db, topic_id=selected_topic_id)
            return
        await state.set_state(StudentFlow.learning_incorrect_options)
        await message.answer(
            "Ответ неверный. Отправьте фото ответа ещё раз, либо нажмите «Подсказка» или «Пропустить задание».",
            reply_markup=learning_incorrect_keyboard(),
        )
        return

    await db.save_answer(student.id, task_id, "learning", answer_image_file_id=file_id, is_correct=True)
    selected_topic_id = int(state_data.get("selected_topic_id", 0)) or None
    await state.clear()
    if selected_topic_id is not None:
        await state.update_data(selected_topic_id=selected_topic_id)
    await message.answer("Отлично! Ответ верный 🎉", reply_markup=learning_after_answer_keyboard())


async def get_task_id_or_reset(message: Message, state: FSMContext) -> int | None:
    state_data = await state.get_data()
    task_id = state_data.get("task_id")
    if not task_id:
        await state.clear()
        await message.answer("Не удалось определить текущее задание. Выберите режим заново.")
        return None
    return int(task_id)


def format_hint_for_student(hint: str) -> str:
    text = hint.strip()
    if not text:
        return "Подсказка пока не добавлена для этого задания."

    replacements = {
        "\\left(": "(",
        "\\right)": ")",
        "\\left[": "[",
        "\\right]": "]",
        "\\left\\{": "{",
        "\\right\\}": "}",
        "\\cdot": "·",
        "\\,": " ",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)

    text = re.sub(r"\\(ln|sin|cos|tan|exp)\b", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return f"Подсказка: {text}"


def prepare_latex_for_render(value: str) -> str:
    prepared = clean_student_text(value)
    prepared = prepared.replace("\\-", "-")
    return prepared


def clean_student_text(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("$") and cleaned.endswith("$") and len(cleaned) >= 2:
        cleaned = cleaned[1:-1].strip()
    cleaned = cleaned.replace("$", "")
    if cleaned.lower().startswith("подсказка:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    return cleaned


async def finish_progress_message(progress_message: Message, check_result) -> None:
    try:
        await progress_message.delete()
    except Exception:
        pass


def extract_formula_from_task_text(task_text: str) -> str:
    prefix = "Вычислите интеграл:"
    text = task_text.strip()
    if text.startswith(prefix):
        return text[len(prefix):].strip()
    return text


async def check_student_answer(
    message: Message,
    llm: GeminiClient,
    db: Database,
    file_id: str,
    expected_answer: str,
    task_text: str,
):
    if not llm.enabled:
        await message.answer("Проверка ответов временно недоступна: LLM не настроена.")
        return None

    try:
        image_bytes, mime_type = await download_telegram_image(message, file_id)
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        image_data_uri = f"data:{mime_type};base64,{image_base64}"
        prompt_template = await db.get_prompt_template(
            "answer_check_final",
            DEFAULT_ANSWER_CHECK_PROMPT_TEMPLATE,
        )
        return await llm.check_student_answer(
            image_data_uri,
            expected_answer=expected_answer,
            task_text=task_text,
            prompt_template=prompt_template,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM answer check failed: file_id=%s task_text=%s", file_id, task_text[:200])
        await message.answer(f"Не удалось проверить ответ через LLM: {exc}")
        return None


async def download_telegram_image(message: Message, file_id: str) -> tuple[bytes, str]:
    telegram_file = await message.bot.get_file(file_id)
    destination = io.BytesIO()
    await message.bot.download_file(telegram_file.file_path, destination=destination)
    mime_type = "image/jpeg"
    if message.document and message.document.file_id == file_id and message.document.mime_type:
        mime_type = message.document.mime_type
    return destination.getvalue(), mime_type


def extract_image_file_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id
    if message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        return message.document.file_id
    return None


async def get_student_or_notify(message: Message, db: Database) -> Student | None:
    user = message.from_user
    if not user:
        return None

    student = await db.get_student_by_telegram_id(user.id)
    if not student and message.chat:
        student = await db.get_student_by_telegram_id(message.chat.id)

    if not student:
        await message.answer(
            "Доступ запрещён: студент не найден в базе. "
            f"Проверьте telegram_user_id (from_user={user.id}, chat={message.chat.id if message.chat else 'n/a'})."
        )
        return None
    return student


async def get_teacher_or_notify(message: Message, db: Database) -> Teacher | None:
    user = message.from_user
    if not user:
        return None
    teacher = await db.get_teacher_by_telegram_id(user.id)
    if not teacher:
        await message.answer("Доступ запрещён: команда доступна только преподавателю.")
        return None
    return teacher


async def get_teacher_from_callback(callback: CallbackQuery, db: Database) -> Teacher | None:
    if not callback.from_user:
        return None
    return await db.get_teacher_by_telegram_id(callback.from_user.id)


async def get_student_from_callback_or_notify(callback: CallbackQuery, db: Database) -> Student | None:
    if not callback.from_user:
        return None

    student = await db.get_student_by_telegram_id(callback.from_user.id)
    if not student and callback.message and callback.message.chat:
        student = await db.get_student_by_telegram_id(callback.message.chat.id)

    if not student:
        await callback.answer(
            "Доступ запрещён: студент не найден в базе. "
            f"from_user={callback.from_user.id}",
            show_alert=True,
        )
        return None
    return student


def normalize_formula(formula: str) -> str:
    normalized = formula.strip().lower()
    normalized = normalized.replace('\\left', '').replace('\\right', '').replace('\\,', '')
    normalized = re.sub(r"\(\s*([a-z0-9]+)\s*\)", r"\1", normalized)
    return "".join(normalized.split())
