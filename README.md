# WebRTC Quality Inspector

Веб-интерфейс для загрузки JSON/JSONL-логов `RTCPeerConnection.getStats()`, расчёта сетевых метрик и быстрой оценки качества звонка.

## Возможности

- загрузка логов до 20 МБ (настраивается через `MAX_UPLOAD_MB`);
- потери пакетов, джиттер, RTT и битрейт по RTP-потокам;
- итоговая оценка качества от 1 до 5;
- график динамики и таблица последних 200 записей;
- временный файл удаляется сразу после обработки;
- health check в `GET /health`.

Подробная схема компонентов, поток данных, границы безопасности и варианты масштабирования описаны в [ARCHITECTURE.md](ARCHITECTURE.md).

## Требования к серверу

Приложение не использует базу данных и не хранит загруженные логи после обработки. Основная нагрузка приходится на CPU во время разбора JSON и на оперативную память, пока формируется результат.

| Профиль | CPU | RAM | Диск | Назначение |
|---|---:|---:|---:|---|
| Минимальный | 1 vCPU | 1 ГБ | 10 ГБ SSD | тестовый стенд, 1 пользователь, небольшие логи |
| Рекомендуемый | 2 vCPU | 2 ГБ | 20 ГБ SSD | рабочий сервер, несколько последовательных проверок |
| Повышенная нагрузка | 4 vCPU | 4–8 ГБ | 30 ГБ SSD | несколько одновременных загрузок и логи близкие к лимиту |

Рекомендуемая ОС — Ubuntu Server 22.04/24.04 LTS x86-64. Также потребуются:

- публичный IPv4/IPv6 или доступ из корпоративной сети;
- DNS-имя, например `webrtc.example.ru`, направленное на сервер;
- открытые входящие порты `22/tcp`, `80/tcp` и `443/tcp`;
- Docker 20.10+;
- Nginx и TLS-сертификат для публичного размещения.

Объём диска почти не растёт со временем: файлы создаются во временном каталоге только на время запроса. При увеличении `MAX_UPLOAD_MB` необходимо пропорционально увеличить запас RAM и `client_max_body_size` в Nginx. Текущий контейнер запускает два Gunicorn worker, поэтому для production рекомендуется не менее 2 vCPU и 2 ГБ RAM.

## Пошаговая установка на сервер

Ниже приведён вариант для чистого Ubuntu Server. Все команды выполняются пользователем с правами `sudo`. Замените `<repository-url>`, `webrtc.example.ru` и адрес электронной почты своими значениями.

### 1. Подготовить DNS и подключиться к серверу

Создайте A/AAAA-запись домена, указывающую на сервер, затем подключитесь:

```bash
ssh deploy@SERVER_IP
```

До выпуска сертификата убедитесь, что домен уже разрешается в адрес сервера:

```bash
getent hosts webrtc.example.ru
```

### 2. Обновить систему и установить системные пакеты

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y docker.io git nginx certbot python3-certbot-nginx curl ufw openssl
sudo systemctl enable --now docker nginx
```

Проверьте установку:

```bash
sudo docker version
sudo nginx -t
```

### 3. Настроить firewall

Сначала разрешите SSH, чтобы не потерять доступ к серверу, затем HTTP/HTTPS:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status
```

Порт `8000` открывать во внешний мир не нужно: контейнер будет слушать его только на `127.0.0.1`.

### 4. Скачать приложение

```bash
sudo mkdir -p /opt/webrtc-quality
sudo chown "$USER":"$USER" /opt/webrtc-quality
git clone <repository-url> /opt/webrtc-quality/app
cd /opt/webrtc-quality/app
```

Для воспроизводимого production-развёртывания рекомендуется переключиться на конкретный tag или commit:

```bash
git checkout <release-tag-or-commit>
```

### 5. Собрать Docker-образ

```bash
sudo docker build --pull -t webrtc-quality:latest .
```

Проверьте, что образ появился локально:

```bash
sudo docker image inspect webrtc-quality:latest --format '{{.Id}}'
```

### 6. Запустить контейнер

Сгенерируйте секрет и сохраните настройки в файле, доступном только администратору:

```bash
sudo install -m 600 /dev/null /opt/webrtc-quality/app.env
SECRET_KEY="$(openssl rand -hex 32)"
printf 'MAX_UPLOAD_MB=20\nSECRET_KEY=%s\n' "$SECRET_KEY" | sudo tee /opt/webrtc-quality/app.env >/dev/null
```

Запустите приложение с автоматическим перезапуском после reboot или сбоя:

```bash
sudo docker run -d \
  --name webrtc-quality \
  --restart unless-stopped \
  --env-file /opt/webrtc-quality/app.env \
  --publish 127.0.0.1:8000:8000 \
  --memory 1g \
  --cpus 2 \
  webrtc-quality:latest
```

Проверьте состояние и health endpoint:

```bash
sudo docker ps --filter name=webrtc-quality
sudo docker logs --tail 50 webrtc-quality
curl --fail http://127.0.0.1:8000/health
```

Ожидаемый ответ: `{"status":"ok"}`.

### 7. Настроить Nginx

Создайте virtual host:

```bash
sudo tee /etc/nginx/sites-available/webrtc-quality >/dev/null <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name webrtc.example.ru;

    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 5s;
        proxy_read_timeout 120s;
    }
}
NGINX

sudo ln -sfn /etc/nginx/sites-available/webrtc-quality /etc/nginx/sites-enabled/webrtc-quality
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

На этом этапе приложение должно открываться по `http://webrtc.example.ru`.

### 8. Включить HTTPS

После того как HTTP-адрес доступен и DNS обновился, запросите сертификат Let's Encrypt:

```bash
sudo certbot --nginx \
  -d webrtc.example.ru \
  --redirect \
  --agree-tos \
  --no-eff-email \
  -m admin@example.ru
```

Проверьте автоматическое продление:

```bash
sudo certbot renew --dry-run
```

### 9. Выполнить финальную проверку

```bash
curl --fail https://webrtc.example.ru/health
sudo docker inspect webrtc-quality --format '{{.State.Status}} / {{if .State.Health}}{{.State.Health.Status}}{{end}}'
```

После этого откройте `https://webrtc.example.ru`, загрузите тестовый JSON/JSONL-лог и убедитесь, что появились оценки аудио, видео и сетевых показателей.

## Обновление и откат

Перед обновлением сохраните идентификатор работающего образа:

```bash
OLD_IMAGE="$(sudo docker inspect webrtc-quality --format '{{.Image}}')"
cd /opt/webrtc-quality/app
git fetch --tags
git checkout <new-release-tag-or-commit>
sudo docker build --pull -t webrtc-quality:latest .
sudo docker rm -f webrtc-quality
```

Затем повторите команду `docker run` из шага 6 и проверьте `/health`. Если новая версия не запускается, выполните откат:

```bash
sudo docker rm -f webrtc-quality
sudo docker run -d \
  --name webrtc-quality \
  --restart unless-stopped \
  --env-file /opt/webrtc-quality/app.env \
  --publish 127.0.0.1:8000:8000 \
  --memory 1g --cpus 2 \
  "$OLD_IMAGE"
```

Постоянные пользовательские данные не хранятся, поэтому отдельное резервное копирование приложения не требуется. Сохраните вне сервера только файл конфигурации Nginx, `app.env` и используемый release tag/commit. Содержимое `app.env` нельзя добавлять в Git.

## Диагностика установки

| Симптом | Команда проверки | Возможная причина |
|---|---|---|
| `502 Bad Gateway` | `sudo docker logs webrtc-quality` | контейнер остановлен или приложение не слушает порт 8000 |
| `413 Request Entity Too Large` | `sudo nginx -T \| grep client_max_body_size` | лимит Nginx меньше размера файла |
| Домен не открывается | `getent hosts webrtc.example.ru` | DNS ещё не обновился или указывает не на тот IP |
| Сертификат не выпускается | `sudo certbot certificates` | порт 80 закрыт или DNS настроен неверно |
| Контейнер перезапускается | `sudo docker inspect webrtc-quality` | нехватка RAM, ошибка конфигурации или приложения |

Для просмотра журнала в реальном времени используйте:

```bash
sudo docker logs --follow --tail 100 webrtc-quality
```

## Локальный запуск

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
flask --app web_app run --host 0.0.0.0 --port 8000
```

Форматы входных данных и CLI-режим описаны в docstring `webrtc_stats_parser.py`. Поддерживаются массив снимков, JSONL и одиночный RTCStatsReport.

## Тесты

```bash
pip install pytest
pytest -q
```
