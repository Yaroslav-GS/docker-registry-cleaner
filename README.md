# Docker Registry Cleanup Script

Автоматический скрипт для очистки старых образов в Docker Registry с поддержкой защищенных тегов и паттернов.

## Возможности

- Удаление образов старше заданного количества дней
- Защита тегов по точному совпадению и паттернам (например, `develop`, `master`)
- Автоматический запуск garbage collection
- Подробное логирование всех операций
- Измерение освобожденного места

## Требования

- Python 3.6+
- Библиотека `requests`: `pip install requests`
- Доступ к Docker на хост-машине
- Запущенный контейнер Docker Registry

## Конфигурация

Создайте файл `config.json`:

```

{
  "registry": {
    "url": "http://127.0.0.1:5001",
    "user": "someuser",
    "password": "somepass",
    "container": "registry"
  },
  "cleanup": {
    "days_to_keep": 30,
    "protected_tags": ["latest", "prod-stable", "dev-stable"],
    "protected_patterns": ["develop", "master", "main", "release"]
  },
  "paths": {
    "config": "/etc/distribution/config.yml",
    "storage": "/var/lib/registry",
    "host_storage": "/some/path"
  }
}

```

### Описание параметров

**Registry:**
- `url` — адрес Docker Registry API
- `user` / `password` — учетные данные для Basic Auth
- `container` — имя Docker-контейнера с registry

**Cleanup:**
- `days_to_keep` — сколько дней хранить образы
- `protected_tags` — список тегов для полной защиты (точное совпадение)
- `protected_patterns` — паттерны для защиты (поиск подстроки, например `develop-289sa1s`)

**Paths:**
- `config` — путь к config.yml registry внутри контейнера
- `storage` — путь к данным registry внутри контейнера
- `host_storage` — путь к данным registry на хост-системе (опционально)

## Использование

### Обычный запуск

```

python3 clean-registry.py

```

### Режим отладки

```

DEBUG=true python3 clean-registry.py

```

## Логирование

Логи сохраняются в `/var/logs/clean-registry.log` и дублируются в stdout.

**Уровни логирования:**
- `INFO` — основные операции (по умолчанию)
- `DEBUG` — детальная информация о манифестах, ошибках (режим `DEBUG=true`)

Формат: `YYYY-MM-DD HH:MM:SS [LEVEL] message`

## Как работает скрипт

1. **Загружает конфигурацию** из `config.json`
2. **Получает список репозиториев** через Registry API
3. **Для каждого тега:**
   - Проверяет защищенные теги и паттерны
   - Получает дату создания образа из манифеста
   - Удаляет теги старше `days_to_keep` дней
4. **Запускает garbage collection** через `docker exec`
5. **Измеряет освобожденное место**

## Защита тегов

Теги защищаются двумя способами:

- **Точное совпадение:** `"protected_tags": ["latest", "prod-stable"]`
- **Поиск подстроки:** `"protected_patterns": ["develop", "master"]`
  - Защитит: `develop-289sa1s`, `master-branch`, `my-develop-build`

## Примечания

- Скрипт требует прав для выполнения `docker exec`
- Garbage collection удаляет только неиспользуемые слои
- Параметр `host_storage` можно убрать, если нет прямого доступа к volume
