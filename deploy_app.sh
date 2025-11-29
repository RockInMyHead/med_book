#!/bin/bash

# Скрипт для деплоя приложения на сервер

echo "🚀 Начинаем деплой приложения..."

# Обновление системы
echo "📦 Обновление системы..."
apt update && apt upgrade -y

# Установка Python и pip
echo "🐍 Установка Python и зависимостей..."
apt install -y python3 python3-pip python3-venv

# Установка дополнительных пакетов для обработки PDF и изображений
echo "📚 Установка системных зависимостей..."
apt install -y libpoppler-cpp-dev libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender-dev libgomp1

# Создание директории для приложения
echo "📁 Создание директории приложения..."
mkdir -p /opt/diagix_books_web
cd /opt/diagix_books_web

echo "✅ Сервер готов к приему приложения!"
echo "📤 Теперь загрузите файлы проекта на сервер"
