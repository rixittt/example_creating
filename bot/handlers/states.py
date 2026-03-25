from aiogram.fsm.state import State, StatesGroup


class StudentFlow(StatesGroup):
    choosing_topic = State()
    showing_theory = State()
    waiting_learning_answer = State()
    waiting_learning_retry_answer = State()
    waiting_testing_answer = State()
    learning_incorrect_options = State()


class TeacherCreateFlow(StatesGroup):
    waiting_topic = State()
    waiting_mode = State()
    waiting_count = State()
    reviewing_generated = State()
