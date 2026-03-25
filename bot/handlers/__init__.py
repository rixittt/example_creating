from bot.handlers.student import router as student_router
from bot.handlers.system import router as system_router
from bot.handlers.teacher import router as teacher_router

routers = (system_router, teacher_router, student_router)

__all__ = ["routers", "student_router", "system_router", "teacher_router"]