## Запуск

Подготовьте `.env` с административными и рабочими реквизитами БД, как описано
в [README.md](README.md), затем выполните из корня проекта:

```powershell
docker compose up -d --wait db
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Тесты создают отдельную схему `fm_test_*` и временные папки. После выполнения
удаляются только тестовая схема и временные файлы; рабочая `public` не очищается.
Создание тестовой схемы требует административных реквизитов из `.env`.
Ожидаемый результат — `Ran 25 tests` и `OK`.

Только модульные тесты форматов, без PostgreSQL:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_formats.py -v
```

## Тесты для задания 2

В Git Bash запустите:

```sh
sh ci/run-local.sh
```

Конвейер выполняет Build → Test → SAST → Deploy в контейнерах. Test сохраняет
JUnit XML, Semgrep блокирует HIGH/CRITICAL правила, Deploy ожидает готовности
БД и приложения через `/health` и сохраняет Docker-образ. Кэш зависимостей,
секреты окружения и передача артефактов настроены в workflow.

Инструкция по CI, артефактам и удалению демонстрационных контейнеров —
[CI.md](CI.md).
