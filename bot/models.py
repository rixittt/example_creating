from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Teacher:
    id: int
    name: str
    telegram_user_id: int


@dataclass(slots=True)
class Student:
    id: int
    name: str
    telegram_user_id: int
    group_id: int
    student_number: int
    group_name: str
    group_number: int
    teacher_id: int


@dataclass(slots=True)
class Topic:
    id: int
    title: str
    llm_prompt: str


@dataclass(slots=True)
class Task:
    id: int
    topic_title: str
    mode: str
    task_text: str
    task_hint_text: str | None
    task_answer_text: str | None
    task_image_file_id: str | None


@dataclass(slots=True)
class TheoryPage:
    id: int
    page_order: int
    title: str
    text_content: str
    image_file_id: str | None
