from __future__ import annotations

import json
import logging
import math
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "data" / "gi-bran.sqlite3"
MAX_REQUEST_SIZE = 16 * 1024 * 1024
LOGIN_USERNAME = "Gisela"
LOGIN_PASSWORD = "32905964"
SESSIONS: set[str] = set()


@contextmanager
def connect_database(database: Path = DATABASE) -> Iterator[sqlite3.Connection]:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(database: Path = DATABASE) -> None:
    with connect_database(database) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS works (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_date TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )


def get_works(database: Path = DATABASE) -> list[dict[str, Any]]:
    with connect_database(database) as connection:
        rows = connection.execute(
            "SELECT payload FROM works ORDER BY id"
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _insert_work(
    connection: sqlite3.Connection, payload: dict[str, Any]
) -> None:
    connection.execute(
        "INSERT INTO works (id, appointment_date, payload) VALUES (?, ?, ?)",
        (
            payload["id"],
            str(payload.get("appointmentDate", "")),
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def import_works(
    legacy_works: list[Any], database: Path = DATABASE
) -> tuple[int, int]:
    if not isinstance(legacy_works, list):
        raise ValueError("Los datos guardados anteriormente no tienen un formato válido.")

    imported = 0
    duplicates = 0
    with connect_database(database) as connection:
        next_id = connection.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM works"
        ).fetchone()[0]

        for item in legacy_works:
            if not isinstance(item, dict):
                raise ValueError("Hay un trabajo guardado con un formato no válido.")

            payload = dict(item)
            work_id = payload.get("id")
            if isinstance(work_id, bool) or not isinstance(work_id, (int, float)):
                work_id = None
            elif isinstance(work_id, float):
                if not math.isfinite(work_id) or not work_id.is_integer():
                    work_id = None
                else:
                    work_id = int(work_id)
            if work_id is not None and (
                work_id <= 0 or work_id > 9_223_372_036_854_775_807
            ):
                work_id = None
            else:
                work_id = int(work_id) if work_id is not None else None

            if work_id is not None:
                existing = connection.execute(
                    "SELECT payload FROM works WHERE id = ?", (work_id,)
                ).fetchone()
                if existing:
                    if json.loads(existing["payload"]) == payload:
                        duplicates += 1
                        continue
                    work_id = None

            if work_id is None:
                while connection.execute(
                    "SELECT 1 FROM works WHERE id = ?", (next_id,)
                ).fetchone():
                    next_id += 1
                work_id = next_id
                next_id += 1

            payload["id"] = work_id
            _insert_work(connection, payload)
            next_id = max(next_id, work_id + 1)
            imported += 1

    return imported, duplicates


def _validate_new_work(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("Los datos del trabajo no tienen un formato válido.")

    required = ("firstName", "lastName", "phone", "price", "deliveryDate", "appointmentDate")
    if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in required):
        raise ValueError("Completa todos los campos obligatorios.")

    try:
        price = Decimal(payload["price"])
    except InvalidOperation as error:
        raise ValueError("El precio debe ser un número válido.") from error
    if not price.is_finite() or price < 0:
        raise ValueError("El precio debe ser un número igual o mayor que cero.")

    try:
        datetime.strptime(payload["deliveryDate"], "%Y-%m-%d")
        datetime.fromisoformat(payload["appointmentDate"])
    except ValueError as error:
        raise ValueError("Revisa las fechas del trabajo.") from error

    photo = payload.get("photo", "")
    description = payload.get("description", "")
    if not isinstance(photo, str) or not isinstance(description, str):
        raise ValueError("La foto o la descripción no tienen un formato válido.")

    return {
        "firstName": payload["firstName"].strip(),
        "lastName": payload["lastName"].strip(),
        "phone": payload["phone"].strip(),
        "description": description.strip(),
        "price": payload["price"],
        "deliveryDate": payload["deliveryDate"],
        "appointmentDate": payload["appointmentDate"],
        "photo": photo,
    }


def create_work(payload: Any, database: Path = DATABASE) -> dict[str, Any]:
    work = _validate_new_work(payload)
    with connect_database(database) as connection:
        overlap = connection.execute(
            "SELECT payload FROM works WHERE appointment_date = ? LIMIT 1",
            (work["appointmentDate"],),
        ).fetchone()
        if overlap:
            existing = json.loads(overlap["payload"])
            raise ValueError(
                "La cita se superpone con la cita de "
                f"{existing.get('firstName', '')} {existing.get('lastName', '')}. "
                "Elige otra fecha u hora."
            )

        cursor = connection.execute(
            "INSERT INTO works (appointment_date, payload) VALUES (?, '{}')",
            (work["appointmentDate"],),
        )
        work["id"] = cursor.lastrowid
        connection.execute(
            "UPDATE works SET payload = ? WHERE id = ?",
            (json.dumps(work, ensure_ascii=False), work["id"]),
        )
    return work


def update_work(payload: Any, database: Path = DATABASE) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Los datos del trabajo no tienen un formato válido.")

    work_id = payload.get("id")
    if isinstance(work_id, bool) or not isinstance(work_id, (int, str)):
        raise ValueError("El identificador del trabajo no es válido.")
    try:
        work_id = int(work_id)
    except (TypeError, ValueError) as error:
        raise ValueError("El identificador del trabajo no es válido.") from error
    if work_id <= 0:
        raise ValueError("El identificador del trabajo no es válido.")

    work = _validate_new_work(payload)
    work["id"] = work_id

    with connect_database(database) as connection:
        existing = connection.execute(
            "SELECT payload FROM works WHERE id = ?",
            (work_id,),
        ).fetchone()
        if not existing:
            raise ValueError("No se encontró el trabajo que intentas actualizar.")

        overlap = connection.execute(
            "SELECT payload FROM works WHERE id != ? AND appointment_date = ? LIMIT 1",
            (work_id, work["appointmentDate"]),
        ).fetchone()
        if overlap:
            existing = json.loads(overlap["payload"])
            raise ValueError(
                "La cita se superpone con la cita de "
                f"{existing.get('firstName', '')} {existing.get('lastName', '')}. "
                "Elige otra fecha u hora."
            )

        connection.execute(
            "UPDATE works SET appointment_date = ?, payload = ? WHERE id = ?",
            (work["appointmentDate"], json.dumps(work, ensure_ascii=False), work_id),
        )
    return work


def delete_work(work_id: Any, database: Path = DATABASE) -> dict[str, Any]:
    try:
        identifier = int(work_id)
    except (TypeError, ValueError) as error:
        raise ValueError("El identificador del trabajo no es válido.") from error
    if identifier <= 0:
        raise ValueError("El identificador del trabajo no es válido.")

    with connect_database(database) as connection:
        row = connection.execute(
            "SELECT payload FROM works WHERE id = ?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise ValueError("No se encontró el trabajo que intentas borrar.")
        deleted = json.loads(row["payload"])
        connection.execute("DELETE FROM works WHERE id = ?", (identifier,))
    return deleted


class AtelierRequestHandler(BaseHTTPRequestHandler):
    server_version = "GI-BRAN/1.0"

    def _send_json(
        self,
        data: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("La solicitud no tiene un tamaño válido.") from error
        if length < 1 or length > MAX_REQUEST_SIZE:
            raise ValueError("La solicitud está vacía o supera el tamaño permitido.")
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("El contenido de la solicitud no es JSON válido.") from error

    def _has_session(self) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        session = cookie.get("gi_bran_session")
        return bool(session and session.value in SESSIONS)

    def _unauthorized(self) -> None:
        self._send_json(
            {"error": "Inicia sesión para continuar."}, HTTPStatus.UNAUTHORIZED
        )

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/works":
            if not self._has_session():
                self._unauthorized()
                return
            self._send_json({"works": get_works()})
            return

        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        asset = files.get(path)
        if not asset:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = (ROOT / asset[0]).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", asset[1])
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_PUT(self) -> None:
        path = urlsplit(self.path).path
        if not path.startswith("/api/works/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._has_session():
            self._unauthorized()
            return
        work_id = path.rsplit("/", 1)[-1]
        if not work_id or not work_id.isdigit():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        try:
            work = update_work({**payload, "id": int(work_id)})
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"work": work})

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        if not path.startswith("/api/works/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._has_session():
            self._unauthorized()
            return
        work_id = path.rsplit("/", 1)[-1]
        if not work_id or not work_id.isdigit():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            deleted = delete_work(work_id)
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"deleted": deleted})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            payload = self._read_json()
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/login":
            if (
                not isinstance(payload, dict)
                or payload.get("username") != LOGIN_USERNAME
                or payload.get("password") != LOGIN_PASSWORD
            ):
                self._send_json(
                    {"error": "Usuario o contraseña incorrectos."},
                    HTTPStatus.UNAUTHORIZED,
                )
                return
            session = secrets.token_urlsafe(32)
            SESSIONS.add(session)
            self._send_json(
                {"ok": True},
                headers={
                    "Set-Cookie": (
                        f"gi_bran_session={session}; HttpOnly; SameSite=Strict; Path=/"
                    )
                },
            )
            return

        if path == "/api/logout":
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except CookieError:
                cookie = SimpleCookie()
            session = cookie.get("gi_bran_session")
            if session:
                SESSIONS.discard(session.value)
            self._send_json(
                {"ok": True},
                headers={
                    "Set-Cookie": (
                        "gi_bran_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
                    )
                },
            )
            return

        if path in ("/api/migrate", "/api/works") and not self._has_session():
            self._unauthorized()
            return

        if path == "/api/migrate":
            if not isinstance(payload, dict):
                self._send_json(
                    {"error": "Los datos guardados anteriormente no tienen un formato válido."},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                imported, duplicates = import_works(payload.get("works"))
            except ValueError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"imported": imported, "duplicates": duplicates})
            return

        if path == "/api/works":
            try:
                work = create_work(payload)
            except ValueError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"work": work}, HTTPStatus.CREATED)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        logging.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    initialize_database()
    server = ThreadingHTTPServer(("127.0.0.1", 8000), AtelierRequestHandler)
    print("GI-BRAN está disponible en http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
