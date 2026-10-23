"""Подключается к PostgreSQL и хранит пользователей, сведения о файлах и журнал действий."""

import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

PROJECT_DIR = Path(__file__).resolve().parent.parent


def load_env():
    """Загружает настройки из .env, сохраняя значения, уже заданные в окружении."""
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if value[:1] in ("'", '"') and value[-1:] == value[:1]:
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def password_key(password, salt):
    """Вычисляет хеш пароля с солью через scrypt, чтобы затруднить перебор паролей."""
    return hashlib.scrypt(password.encode(), salt=salt.encode(), n=32768,
                          r=8, p=3, dklen=64, maxmem=64 * 1024 * 1024).hex()


def check_password(password, stored_hash):
    """Вычисляет хеш введённого пароля и сравнивает его с хешем из БД."""
    parts = stored_hash.split("$")
    if len(parts) != 6 or parts[:4] != ["scrypt", "32768", "8", "3"]:
        raise ValueError("Неподдерживаемый формат хеша в БД.")
    return hmac.compare_digest(password_key(password, parts[4]), parts[5])


class Database:
    """Объединяет запросы к БД и использует одно соединение, пока работает приложение."""

    def __init__(self, schema=None, initialize=False):
        """Открывает соединение с БД; при initialize=True также создаёт схему, таблицы и процедуры."""
        load_env()
        self.schema = schema or os.getenv("PGSCHEMA", "public")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.schema):
            raise ValueError("Некорректное имя схемы PGSCHEMA.")
        prefix = "PGADMIN" if initialize else "PG"
        password = os.getenv(prefix + "PASSWORD")
        if not password:
            raise ValueError(f"Задайте {prefix}PASSWORD в окружении или .env.")
        self.connection = psycopg.connect(
            host=os.getenv("PGHOST", "127.0.0.1"), port=os.getenv("PGPORT", "5432"),
            dbname=os.getenv("PGDATABASE", "filemanager"),
            user=os.getenv(prefix + "USER", "postgres" if initialize else "filemanager_app"), password=password,
            connect_timeout=5, autocommit=True, row_factory=dict_row,
            options=f"-c search_path={self.schema} -c timezone=UTC -c statement_timeout=15000",
        )
        try:
            if not initialize:
                role = self.connection.execute("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user").fetchone()
                if any(role.values()):
                    raise ValueError("Приложению нужна роль без SUPERUSER, CREATEDB и CREATEROLE.")
                self.connection.execute("SELECT id FROM users LIMIT 0")
                return
            with self.connection.transaction():
                # Два запуска не должны одновременно менять одну схему.
                self.connection.execute("SELECT pg_advisory_xact_lock(734911, hashtext(%s))", (self.schema,))
                self.connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
                self.connection.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        except Exception:
            self.close()
            raise

    def lock(self):
        """Не даёт открыть вторую консоль для той же схемы БД до закрытия этого соединения."""
        row = self.connection.execute(
            "SELECT pg_try_advisory_lock(734910, hashtext(%s)) AS locked", (self.schema,)
        ).fetchone()
        if not row["locked"]:
            raise ValueError("Другая копия приложения уже запущена. Закройте её.")

    def close(self):
        """Закрывает соединение с БД и освобождает удерживаемую им блокировку запуска."""
        self.connection.close()

    def unfinished(self, user_id):
        """Находит операции, для которых в журнале есть начало, но нет записи об успехе или ошибке."""
        return self.connection.execute("""
            SELECT id, operation_type, timestamp FROM operations s
            WHERE user_id=%s AND status='started'
              AND operation_type <> 'reconcile'
              AND details ? 'operation_id'
              AND NOT EXISTS (SELECT 1 FROM operations e
                WHERE e.user_id=s.user_id AND e.status IN ('success','failed')
                  AND e.details->>'operation_id'=s.details->>'operation_id')
            ORDER BY id
            """, (user_id,)).fetchall()

    def check(self):
        """Возвращает версию PostgreSQL, выбранную схему и список хранимых процедур."""
        version = self.connection.execute("SHOW server_version").fetchone()["server_version"]
        procedures = self.connection.execute("""
            SELECT routine_name FROM information_schema.routines
            WHERE routine_schema=%s AND routine_type='PROCEDURE' ORDER BY routine_name
            """, (self.schema,)).fetchall()
        return {"PostgreSQL": version, "schema": self.schema, "procedures": procedures}

    def log(self, user_id, action, status, details=None, file_id=None):
        # Значения идут отдельно от SQL: %s — параметр, а не подстановка строк.
        """Добавляет событие в журнал БД через процедуру record_operation."""
        self.connection.execute("CALL record_operation(%s, %s, %s, %s, %s)",
                                (user_id, action, status, Jsonb(details or {}), file_id))

    def register(self, username, password):
        """Проверяет логин и пароль и создаёт пользователя, сохраняя хеш вместо самого пароля."""
        if not re.fullmatch(r"[a-zA-Z0-9_-]{3,32}", username):
            raise ValueError("Логин: 3–32 латинских буквы, цифры, _ или -.")
        if len(password) < 8 or len(password.encode()) > 128:
            raise ValueError("Пароль: минимум 8 символов, максимум 128 байт.")
        salt = secrets.token_hex(16)
        stored_hash = f"scrypt$32768$8$3${salt}${password_key(password, salt)}"
        try:
            with self.connection.transaction():
                user = self.connection.execute(
                    "INSERT INTO users(username, password_hash) VALUES (%s, %s) RETURNING id, username",
                    (username, stored_hash)).fetchone()
                self.log(user["id"], "register", "success")
            return user
        except psycopg.errors.UniqueViolation:
            raise ValueError("Этот логин уже занят.") from None

    def login(self, username, password):
        """Проверяет пароль, учитывает неудачные попытки входа и возвращает ID и логин пользователя."""
        if not re.fullmatch(r"[a-zA-Z0-9_-]{3,32}", username) or len(password.encode()) > 128:
            raise ValueError("Некорректный логин или слишком длинный пароль.")
        error = None
        with self.connection.transaction():
            # FOR UPDATE защищает счётчик от одновременных попыток входа.
            user = self.connection.execute("SELECT * FROM users WHERE username=%s FOR UPDATE", (username,)).fetchone()
            now = int(time.time() * 1000)
            if user and user["locked_until"] > now:
                error = "Вход временно заблокирован. Подождите до одной минуты."
            else:
                dummy_hash = "scrypt$32768$8$3$" + "0" * 32 + "$" + "0" * 128
                valid = check_password(password, user["password_hash"] if user else dummy_hash)
                if not valid or not user:
                    error = "Неверный логин или пароль."
                    if user:
                        attempts = (0 if user["locked_until"] else user["failed_attempts"]) + 1
                        locked_until = now + 60_000 if attempts >= 5 else 0
                        self.connection.execute("UPDATE users SET failed_attempts=%s, locked_until=%s WHERE id=%s",
                                                (attempts, locked_until, user["id"]))
                else:
                    self.connection.execute("UPDATE users SET failed_attempts=0, locked_until=0 WHERE id=%s", (user["id"],))
            self.log(user["id"] if user else None, "login", "failed" if error else "success")
        # Ошибка после выхода из with: счётчик и failed уже сохранены COMMIT.
        if error:
            raise ValueError(error)
        return {"id": user["id"], "username": user["username"]}

    def save_file(self, user_id, location, size, kind):
        """Добавляет или обновляет запись о файле по его владельцу и полному пути; возвращает ID записи."""
        path = Path(location)
        if not path.is_absolute():
            raise ValueError("Местоположение файла должно быть абсолютным путём.")
        location = path.as_posix()
        return self.connection.execute("""
            INSERT INTO files(filename, created_at, updated_at, size, location, kind, owner_id)
            VALUES (%s, clock_timestamp(), clock_timestamp(), %s, %s, %s, %s)
            ON CONFLICT(owner_id, location) DO UPDATE SET
              size=excluded.size, kind=excluded.kind, updated_at=excluded.updated_at, deleted=FALSE,
              created_at=CASE WHEN files.deleted THEN excluded.created_at ELSE files.created_at END
            RETURNING id
            """, (Path(location).name, size, location, kind, user_id)).fetchone()["id"]

    def delete_file(self, user_id, location):
        """Помечает файл удалённым в БД, сохраняя запись для связи с прежними событиями журнала."""
        path = Path(location)
        if not path.is_absolute():
            raise ValueError("Местоположение файла должно быть абсолютным путём.")
        location = path.as_posix()
        return self.connection.execute("CALL soft_delete_file(%s, %s, %s)",
                                       (user_id, location, None)).fetchone()["p_file_id"]

    def files(self, user_id):
        """Возвращает записи о файлах пользователя, включая помеченные удалёнными."""
        return self.connection.execute("""
            SELECT id, filename, location, size, kind, created_at, updated_at, deleted
            FROM files WHERE owner_id=%s ORDER BY location
            """, (user_id,)).fetchall()

    def history(self, user_id, limit=20):
        """Возвращает последние события пользователя; можно запросить от 1 до 100 записей."""
        if not 1 <= limit <= 100:
            raise ValueError("Количество событий: от 1 до 100.")
        return self.connection.execute("""
            SELECT id, timestamp, operation_type, status, file_id, details
            FROM operations WHERE user_id=%s ORDER BY id DESC LIMIT %s
            """, (user_id, limit)).fetchall()
