from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.parse
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

APP_DIR = Path(__file__).resolve().parent
ARSHIN_HOST = "fgis.gost.ru"
ARSHIN_BASE_PATH = "/fundmetrology/eapi"
UPSTREAM_TIMEOUT = 20

app = Flask(__name__, static_folder=None)


def normalized(value: Any) -> str:
    return str(value or "").strip().casefold()


class ArshinHTTPError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class IPv4HTTPSConnection(http.client.HTTPSConnection):
    """HTTPS-соединение с принудительным использованием конкретного IPv4."""

    def __init__(self, host: str, fixed_ip: str, **kwargs: Any):
        self.fixed_ip = fixed_ip
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self.fixed_ip, self.port),
            timeout=self.timeout,
            source_address=self.source_address,
        )
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self.host,
        )


def arshin_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = urllib.parse.urlencode(params or {}, doseq=True)
    request_path = f"{ARSHIN_BASE_PATH}{path}"
    if query:
        request_path = f"{request_path}?{query}"

    # Некоторые облачные сети сначала пытаются подключиться по IPv6 и зависают.
    # Получаем только IPv4-адреса и пробуем их по очереди.
    address_info = socket.getaddrinfo(
        ARSHIN_HOST,
        443,
        family=socket.AF_INET,
        type=socket.SOCK_STREAM,
    )
    ipv4_addresses = list(dict.fromkeys(item[4][0] for item in address_info))

    if not ipv4_addresses:
        raise ConnectionError("Не найден IPv4-адрес ГИС «Аршин».")

    last_error: Exception | None = None
    ssl_context = ssl.create_default_context()

    for ip_address in ipv4_addresses:
        connection = IPv4HTTPSConnection(
            ARSHIN_HOST,
            fixed_ip=ip_address,
            port=443,
            timeout=UPSTREAM_TIMEOUT,
            context=ssl_context,
        )
        try:
            connection.request(
                "GET",
                request_path,
                headers={
                    "Accept": "application/json",
                    "Host": ARSHIN_HOST,
                    "User-Agent": "Mozilla/5.0 ArshinSearchService/1.0",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")

            if response.status != 200:
                raise ArshinHTTPError(response.status, text[:1000])

            return json.loads(text)
        except ArshinHTTPError:
            raise
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            last_error = exc
        finally:
            connection.close()

    raise ConnectionError(
        f"Не удалось подключиться к ГИС «Аршин» по IPv4: {last_error}"
    )


@app.after_request
def add_response_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "arshin-search-service"})


@app.get("/api/search")
def search():
    year = request.args.get("year", "").strip()
    registry = request.args.get("registry", "").strip()
    serial = request.args.get("serial", "").strip()

    if not (year.isdigit() and len(year) == 4):
        return jsonify({"ok": False, "error": "Некорректно указан год."}), 400

    if not registry or not serial:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Номер реестра и серийный номер обязательны.",
                }
            ),
            400,
        )

    # Серийный номер обычно намного точнее регистрационного номера.
    # После поиска строго сверяем оба поля.
    matched: list[dict[str, Any]] = []
    upstream_count = 0
    start = 0
    rows = 100
    max_pages = 10

    try:
        for _ in range(max_pages):
            payload = arshin_get(
                "/vri",
                {
                    "year": year,
                    "search": serial,
                    "start": start,
                    "rows": rows,
                },
            )

            result = payload.get("result") or {}
            items = result.get("items") or []
            upstream_count = int(result.get("count") or 0)

            for item in items:
                if (
                    normalized(item.get("mit_number")) == normalized(registry)
                    and normalized(item.get("mi_number")) == normalized(serial)
                ):
                    matched.append(item)

            if matched or len(items) < rows:
                break

            start += rows
            if start >= upstream_count:
                break

        return jsonify(
            {
                "ok": True,
                "year": int(year),
                "query": {"registry": registry, "serial": serial},
                "count": len(matched),
                "upstream_count": upstream_count,
                "items": matched,
            }
        )

    except ArshinHTTPError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"ГИС «Аршин» вернул ошибку HTTP {exc.status}.",
                    "details": exc.body,
                }
            ),
            502,
        )
    except (ConnectionError, OSError, TimeoutError, ssl.SSLError) as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Не удалось подключиться к ГИС «Аршин».",
                    "details": str(exc),
                }
            ),
            502,
        )
    except json.JSONDecodeError:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "ГИС «Аршин» вернул ответ в неожиданном формате.",
                }
            ),
            502,
        )
    except Exception as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Ошибка серверного сервиса.",
                    "details": str(exc),
                }
            ),
            500,
        )


@app.get("/api/details")
def details():
    vri_id = request.args.get("vri_id", "").strip()

    if not vri_id:
        return jsonify({"ok": False, "error": "Не указан идентификатор vri_id."}), 400

    safe_id = urllib.parse.quote(vri_id, safe="-_.~")

    try:
        payload = arshin_get(f"/vri/{safe_id}")
        return jsonify({"ok": True, "data": payload})
    except ArshinHTTPError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"Не удалось загрузить карточку. HTTP {exc.status}.",
                    "details": exc.body,
                }
            ),
            502,
        )
    except (ConnectionError, OSError, TimeoutError, ssl.SSLError) as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Не удалось подключиться к ГИС «Аршин».",
                    "details": str(exc),
                }
            ),
            502,
        )
    except json.JSONDecodeError:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "ГИС «Аршин» вернул ответ в неожиданном формате.",
                }
            ),
            502,
        )
    except Exception as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Ошибка серверного сервиса.",
                    "details": str(exc),
                }
            ),
            500,
        )


@app.get("/<path:filename>")
def project_file(filename: str):
    return send_from_directory(APP_DIR, filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
