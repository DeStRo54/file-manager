"""Отвечает на /health, чтобы Docker мог проверить подключение приложения к БД."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from db.database import Database


class HealthHandler(BaseHTTPRequestHandler):
    """Обрабатывает HTTP-запросы проверки готовности приложения."""
    def do_GET(self):
        """Возвращает 200, если БД доступна, 503 при ошибке и 404 для неизвестного адреса."""
        if self.path != "/health":
            self.send_error(404)
            return
        database = None
        try:
            database = Database()
            database.check()
            status, payload = 200, {"status": "ok"}
        except Exception:
            status, payload = 503, {"status": "unavailable"}
        finally:
            if database is not None:
                database.close()
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), HealthHandler).serve_forever()
