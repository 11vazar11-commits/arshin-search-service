from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

APP_DIR = Path(__file__).resolve().parent
ARSHIN_API = "https://fgis.gost.ru/fundmetrology/eapi"

app = Flask(__name__, static_folder=None)


def normalized(value: Any) -> str:
    return str(value or "").strip().casefold()


def arshin_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{ARSHIN_API}{path}"
    if query:
        url = f"{url}?{query}"

    upstream_request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 ArshinSearchService/1.0",
        },
        method="GET",
    )

    with urllib.request.urlopen(upstream_request, timeout=55) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read().decode(charset, errors="replace")
        return json.loads(raw)


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

    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")[:1000]
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"ГИС «Аршин» вернул ошибку HTTP {exc.code}.",
                    "details": details,
                }
            ),
            502,
        )
    except urllib.error.URLError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Не удалось подключиться к ГИС «Аршин».",
                    "details": str(exc.reason),
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
    except urllib.error.HTTPError as exc:
        details_text = exc.read().decode("utf-8", errors="replace")[:1000]
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"Не удалось загрузить карточку. HTTP {exc.code}.",
                    "details": details_text,
                }
            ),
            502,
        )
    except urllib.error.URLError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Не удалось подключиться к ГИС «Аршин».",
                    "details": str(exc.reason),
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
