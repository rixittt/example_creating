# Math Bot

Telegram-бот для обучения и тестирования по темам математического анализа.

## Возможности

- два пользовательских сценария: преподаватель и студент;
- генерация задач через Gemini API;
- обучение с теорией, подсказками и показом правильного ответа;
- тестирование с прогрессом по выбранной теме;
- хранение задач, ответов, групп и тем в PostgreSQL;

## Стек

- Python 3.10+;
- Aiogram 3;
- PostgreSQL;
- AsyncPG;
- Matplotlib MathText для рендера формул;
- Gemini API для генерации и проверки ответов.

## Подготовка окружения

### 1. Установить зависимости

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Настроить `.env`

Необходимый набор переменных:

```env
BOT_TOKEN=...
DATABASE_URL=postgresql://user:password@localhost:5432/math_bot
GEMINI_API_KEY=...
GEMINI_ENDPOINT=https://api.gen-api.ru/api/v1/networks/gemini-2-5-flash-lite
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_SSL_VERIFY=true
GEMINI_STATUS_ENDPOINT_TEMPLATE=https://api.gen-api.ru/api/v1/request/get/{request_id}
```

### 3. Получить `BOT_TOKEN` через @BotFather

1. В Telegram откройте [@BotFather](https://t.me/BotFather).
2. Отправьте команду `/newbot`.
3. Укажите имя бота (любое отображаемое имя).
4. Укажите username бота (должен заканчиваться на `bot`, например `math_course_helper_bot`).
5. BotFather пришлёт токен вида `123456789:AA...`.
6. Скопируйте его в `.env`:

```env
BOT_TOKEN=123456789:AA...
```

### 4. Получить `GEMINI_API_KEY`

1. перейти на сайт https://gen-api.ru/account/api-tokens
2. зарегистрироваться
3. создать токен
4. добавить в env


## Создание базы данных

### Полное пересоздание БД

```bash
sudo -u postgres psql -c "DROP DATABASE IF EXISTS math_bot;"
sudo -u postgres psql -c "CREATE DATABASE math_bot OWNER enfdb;"
psql "postgresql://enfdb:enfdb@localhost:5432/math_bot" -f db/schema.sql
psql "postgresql://enfdb:enfdb@localhost:5432/math_bot" -f db/seed_teachers.sql
```

## Как получить `file_id` для картинок теории

Если вы хотите прикрепить изображение к странице теории в `db/seed_teachers.sql`, нужен Telegram `file_id`.

1. Запустите **того же самого** бота, который будет использоваться в проекте:

```bash
python run_bot.py
```

2. Отправьте этому боту в чат нужную картинку с командой /fileid.
3. Бот вернёт `file_id` строки вида `AgACAgIAAxkBA...`.
4. Подставьте этот `file_id` в `db/seed_teachers.sql` в поле `image_file_id`.

Важно:
- `file_id` зависит от конкретного бота, поэтому берите его именно у вашего бота;
- после изменения `seed_teachers.sql` перезалейте сиды в БД (или выполните точечный `UPDATE`).


### Если у пользователя БД нет права `CREATEDB`

Создавайте БД под `postgres` или другим superuser.

## Запуск

```bash
python run_bot.py
```