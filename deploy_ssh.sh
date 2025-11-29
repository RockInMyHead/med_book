#!/usr/bin/expect -f

set timeout 30

# Параметры подключения
set host "147.45.232.140"
set user "root"
set password "qQ6H^c7-et5J+S"

# Подключение к серверу
spawn ssh -o StrictHostKeyChecking=no $user@$host

# Ожидание запроса пароля
expect {
    "password:" {
        send "$password\r"
    }
    timeout {
        puts "Timeout waiting for password prompt"
        exit 1
    }
    eof {
        puts "SSH connection failed"
        exit 1
    }
}

# Ожидание приглашения командной строки
expect {
    "#" {
        puts "Successfully connected to server"
    }
    timeout {
        puts "Timeout waiting for shell prompt"
        exit 1
    }
}

# Интерактивная сессия
interact
