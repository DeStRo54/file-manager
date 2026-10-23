-- PostgreSQL 17. Применяется python -m db.init_db при явной подготовке схемы.
CREATE TABLE IF NOT EXISTS Users (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  username VARCHAR(32) NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  failed_attempts INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
  locked_until BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS Files (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  filename TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  size BIGINT NOT NULL CHECK (size >= 0),
  location TEXT NOT NULL CONSTRAINT files_location_absolute CHECK (location ~ '^(/|[A-Za-z]:/)'),
  kind TEXT NOT NULL CHECK (kind IN ('file','directory')),
  owner_id INTEGER NOT NULL REFERENCES Users(id),
  deleted BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE(owner_id, location)
);

CREATE TABLE IF NOT EXISTS Operations (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  timestamp TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  operation_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('started','success','failed')),
  file_id INTEGER REFERENCES Files(id),
  user_id INTEGER REFERENCES Users(id),
  details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS operations_user_time ON Operations(user_id, id);

CREATE OR REPLACE FUNCTION check_operation_owner()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.file_id IS NOT NULL AND
     (NEW.user_id IS NULL OR NEW.user_id IS DISTINCT FROM
       (SELECT owner_id FROM Files WHERE id = NEW.file_id)) THEN
    RAISE EXCEPTION 'File owner does not match operation user'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER operations_owner_check
BEFORE INSERT OR UPDATE ON Operations
FOR EACH ROW EXECUTE FUNCTION check_operation_owner();

-- Все записи аудита приложение добавляет именно через CALL.
CREATE OR REPLACE PROCEDURE record_operation(
  p_user_id INTEGER,
  p_operation_type TEXT,
  p_status TEXT,
  p_details JSONB,
  p_file_id INTEGER
)
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO Operations(operation_type, status, file_id, user_id, details)
  VALUES (p_operation_type, p_status, p_file_id, p_user_id, p_details);
END;
$$;

-- Сохраняет строку файла для внешних ключей прежних событий.
CREATE OR REPLACE PROCEDURE soft_delete_file(
  p_owner_id INTEGER,
  p_location TEXT,
  INOUT p_file_id INTEGER
)
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE Files SET deleted = TRUE, updated_at = clock_timestamp()
  WHERE owner_id = p_owner_id AND location = p_location
  RETURNING id INTO p_file_id;
END;
$$;
