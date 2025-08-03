#!/bin/bash

echo "Застосування схеми до бази даних..."
set -e

# Застосовуємо схему, якщо таблиці вже існують, вона оновиться завдяки "IF NOT EXISTS"
psql $DATABASE_URL -f schema.sql -X

echo "Схема бази даних успішно застосована."

echo "Запуск воркера бота..."
# Запускаємо FastAPI додаток за допомогою uvicorn
exec uvicorn bot:app --host 0.0.0.0 --port $PORT
