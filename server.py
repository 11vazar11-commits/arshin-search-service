from __future__ import annotations

import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = os.environ.get("HOST", "0.0.0.0")
try:
    PORT = int(os.environ.get("PORT", "8000"))
except ValueError:
    PORT = 8000
ARSHIN_API = "https://fgis.gost.ru/fundmetrology/eapi"
PROJECT_DIR = Path(__file__).resolve().parent


def send_json(handler: SimpleHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def arshin_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{ARSHIN_API}{path}"
    if query:
        url = f"{url}?{query}"

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 ArshinSearchService/1.0",
        },
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=35) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read().decode(charset, errors="replace")
        return json.loads(raw)


def normalized(value: Any) -> str:
    return str(value or "").strip().casefold()


class ArshinHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/health":
            send_json(self, 200, {"ok": True, "service": "arshin-search-service"})
            return

        if parsed.path == "/api/search":
            self.handle_search(parsed)
            return

        if parsed.path == "/api/details":
            self.handle_details(parsed)
            return

        if parsed.path == "/":
            self.path = "/index.html"

        super().do_GET()

    def handle_search(self, parsed: urllib.parse.ParseResult) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        year = (query.get("year") or [""])[0].strip()
        registry = (query.get("registry") or [""])[0].strip()
        serial = (query.get("serial") or [""])[0].strip()

        if not (year.isdigit() and len(year) == 4):
            send_json(self, 400, {"ok": False, "error": "Некорректно указан год."})
            return

        if not registry or not serial:
            send_json(
                self,
                400,
                {"ok": False, "error": "Номер реестра и серийный номер обязательны."},
            )
            return

        search_value = f"{registry} {serial}"
        matched: list[dict[str, Any]] = []
        upstream_count = 0
        start = 0
        rows = 100
        max_pages = 25

        try:
            for _ in range(max_pages):
                payload = arshin_get(
                    "/vri",
                    {
                        "year": year,
                        "search": search_value,
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

                if len(items) < rows:
                    break

                start += rows
                if start >= upstream_count:
                    break

            send_json(
                self,
                200,
                {
                    "ok": True,
                    "year": int(year),
                    "query": {"registry": registry, "serial": serial},
                    "count": len(matched),
                    "upstream_count": upstream_count,
                    "items": matched,
                },
            )
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")[:1000]
            send_json(
                self,
                502,
                {
                    "ok": False,
                    "error": f"ГИС «Аршин» вернул ошибку HTTP {exc.code}.",
                    "details": details,
                },
            )
        except urllib.error.URLError as exc:
            send_json(
                self,
                502,
                {
                    "ok": False,
                    "error": "Не удалось подключиться к ГИС «Аршин».",
                    "details": str(exc.reason),
                },
            )
        except json.JSONDecodeError:
            send_json(
                self,
                502,
                {"ok": False, "error": "ГИС «Аршин» вернул ответ в неожиданном формате."},
            )
        except Exception as exc:
            send_json(
                self,
                500,
                {"ok": False, "error": "Ошибка локального сервиса.", "details": str(exc)},
            )

    def handle_details(self, parsed: urllib.parse.ParseResult) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        vri_id = (query.get("vri_id") or [""])[0].strip()

        if not vri_id:
            send_json(self, 400, {"ok": False, "error": "Не указан идентификатор vri_id."})
            return

        safe_id = urllib.parse.quote(vri_id, safe="-_.~")

        try:
            payload = arshin_get(f"/vri/{safe_id}")
            send_json(self, 200, {"ok": True, "data": payload})
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")[:1000]
            send_json(
                self,
                502,
                {
                    "ok": False,
                    "error": f"Не удалось загрузить карточку. HTTP {exc.code}.",
                    "details": details,
                },
            )
        except urllib.error.URLError as exc:
            send_json(
                self,
                502,
                {
                    "ok": False,
                    "error": "Не удалось подключиться к ГИС «Аршин».",
                    "details": str(exc.reason),
                },
            )
        except Exception as exc:
            send_json(
                self,
                500,
                {"ok": False, "error": "Ошибка локального сервиса.", "details": str(exc)},
            )


def main() -> None:
    os.chdir(PROJECT_DIR)
    server = ThreadingHTTPServer((HOST, PORT), ArshinHandler)
    print("")
    print("Сервис запущен.")
    print(f"Адрес для локальной проверки: http://127.0.0.1:{PORT}")
    print("Чтобы остановить сервис, нажмите Ctrl+C.")
    print("")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервис остановлен.")
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except OSError as exc:
        print(f"Не удалось запустить сервис: {exc}")
        print(f"Возможно, порт {PORT} уже занят.")
        sys.exit(1)
