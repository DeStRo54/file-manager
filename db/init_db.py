"""Готовит БД перед первым запуском приложения."""

import os
import re

from psycopg import sql

from .database import Database, load_env


def provision(schema=None):
    """Создаёт схему и настраивает рабочую роль: разрешает нужные запросы, ограничивая изменение таблиц и журнала."""
    load_env()
    username = os.getenv("PGUSER", "filemanager_app")
    password = os.getenv("PGPASSWORD")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", username) or not password:
        raise ValueError("Задайте безопасное имя PGUSER и собственный PGPASSWORD.")
    database = Database(schema=schema, initialize=True)
    try:
        with database.connection.transaction():
            if username == database.connection.execute("SELECT current_user AS name").fetchone()["name"]:
                raise ValueError("Административная и рабочая роли должны отличаться.")
            role = sql.Identifier(username)
            namespace = sql.Identifier(database.schema)
            existing = database.connection.execute("""
                SELECT r.oid FROM pg_roles r WHERE r.rolname=%s AND
                  (EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid)
                   OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspowner=r.oid)
                   OR EXISTS (SELECT 1 FROM pg_database d WHERE d.datdba=r.oid))
                """, (username,)).fetchone()
            if existing:
                raise ValueError("PGUSER владеет базой/схемой или наследует роли. Используйте новую отдельную роль.")
            if not database.connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (username,)).fetchone():
                database.connection.execute(sql.SQL("CREATE ROLE {} LOGIN").format(role))
            database.connection.execute(sql.SQL("ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}").format(role, sql.Literal(password)))
            database.connection.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC, {}").format(namespace, role))
            database.connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(namespace, role))
            database.connection.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(namespace, role))
            database.connection.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(namespace))
            database.connection.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM PUBLIC, {}").format(namespace, role))
            database.connection.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON users, files TO {}").format(role))
            database.connection.execute(sql.SQL("GRANT SELECT ON operations TO {}").format(role))
            database.connection.execute(sql.SQL("GRANT USAGE ON SEQUENCE users_id_seq, files_id_seq TO {}").format(role))
            database.connection.execute("REVOKE ALL ON PROCEDURE record_operation(integer,text,text,jsonb,integer) FROM PUBLIC")
            database.connection.execute("ALTER PROCEDURE record_operation(integer,text,text,jsonb,integer) SECURITY DEFINER")
            database.connection.execute(sql.SQL("ALTER PROCEDURE record_operation(integer,text,text,jsonb,integer) SET search_path TO {}, pg_temp").format(namespace))
            database.connection.execute(sql.SQL("GRANT EXECUTE ON PROCEDURE record_operation(integer,text,text,jsonb,integer) TO {}").format(role))
            database.connection.execute("REVOKE ALL ON PROCEDURE soft_delete_file(integer,text,integer) FROM PUBLIC")
            database.connection.execute(sql.SQL("GRANT EXECUTE ON PROCEDURE soft_delete_file(integer,text,integer) TO {}").format(role))
    finally:
        database.close()


if __name__ == "__main__":
    provision()
    print("Схема подготовлена; рабочая роль не может изменять структуру и журнал.")
