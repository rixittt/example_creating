from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.db import Database
from bot.handlers.common import format_student_display_name
from bot.keyboards.common import student_menu_keyboard, teacher_menu_keyboard

router = Router()


@router.message(Command("fileid"), F.photo)
async def show_photo_file_id(message: Message) -> None:
    await message.answer(f"file_id: <code>{message.photo[-1].file_id}</code>")


@router.message(Command("fileid"), F.document)
async def show_document_file_id(message: Message) -> None:
    if message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        await message.answer(f"file_id: <code>{message.document.file_id}</code>")
        return
    await message.answer("Для команды /fileid отправьте изображение фото или документом.")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database) -> None:
    user = message.from_user
    if not user:
        return

    teacher = await db.get_teacher_by_telegram_id(user.id)
    if teacher:
        await state.clear()
        await message.answer(
            f"Здравствуйте, {teacher.name}! Вы вошли как преподаватель.",
            reply_markup=teacher_menu_keyboard(),
        )
        return

    student = await db.get_student_by_telegram_id(user.id)
    if student:
        await state.clear()
        student_display_name = format_student_display_name(student)
        await message.answer(
            f"Здравствуйте, {student_display_name}! Вы вошли как студент.",
            reply_markup=student_menu_keyboard(),
        )
        return

    await state.clear()
    await message.answer("Ваш Telegram ID не найден в базе.")
