#!/usr/bin/expect -f

set timeout 60

# Параметры подключения
set host "147.45.232.140"
set user "root"
set password "qQ6H^c7-et5J+S"
set archive_path "/Users/artembutko/Downloads/diagix_books_web.tar.gz"

# Подключение к серверу
spawn scp $archive_path $user@$host:/tmp/
expect {
    "password:" {
        send "$password\r"
    }
    timeout {
        puts "Timeout during file upload"
        exit 1
    }
}

expect {
    "100%" {
        puts "File uploaded successfully"
    }
    timeout {
        puts "File upload may have failed"
    }
}

# Подключение для настройки сервера
spawn ssh -o StrictHostKeyChecking=no $user@$host

expect {
    "password:" {
        send "$password\r"
    }
    timeout {
        puts "Timeout waiting for SSH password"
        exit 1
    }
}

expect "#" {
    puts "Connected to server, starting deployment..."
}

# Выполнение команд настройки
send "cd /opt\r"
send "rm -rf diagix_books_web\r"
send "mkdir -p diagix_books_web\r"
send "cd diagix_books_web\r"
send "tar -xzf /tmp/diagix_books_web.tar.gz\r"
send "apt update\r"
send "apt install -y python3 python3-pip python3-venv libpoppler-cpp-dev libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender-dev libgomp1\r"
send "python3 -m venv venv\r"
send "source venv/bin/activate\r"
send "pip install --upgrade pip\r"
send "pip install -r requirements.txt\r"

expect "#" {
    puts "Dependencies installed successfully"
}

# Запуск приложения
send "cd /opt/diagix_books_web\r"
send "source venv/bin/activate\r"
send "python run_web_app.py\r"

expect {
    "Streamlit app in your browser" {
        puts "✅ Application deployed and running successfully!"
    }
    timeout {
        puts "Application startup timeout"
    }
}

# Интерактивная сессия
interact
