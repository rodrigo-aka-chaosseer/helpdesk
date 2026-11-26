import io
import os
import shlex
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from flask import Flask, current_app, jsonify, render_template, request, send_file


ROOT_DIR = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT_DIR / ".localstack" / "test-state.env"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "text/plain",
}


def _read_api_url(state_file=STATE_FILE):
    configured_url = os.environ.get("HELPDESK_API_URL")
    if configured_url:
        return configured_url.strip()

    try:
        lines = state_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""

    for line in lines:
        key, separator, value = line.partition("=")
        if key == "API_URL" and separator:
            parts = shlex.split(value)
            return parts[0] if parts else ""
    return ""


def _validated_http_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return ""
    return value.rstrip("/")


def _api_call(method, path, **kwargs):
    api_base = current_app.config["API_BASE"]
    if not api_base:
        return None, (jsonify({"erro": "API_URL não configurada"}), 503)

    try:
        response = requests.request(
            method,
            f"{api_base}/{path.lstrip('/')}",
            timeout=20,
            **kwargs,
        )
    except requests.RequestException:
        current_app.logger.warning("Falha ao acessar a API Helpdesk (%s %s)", method, path)
        return None, (jsonify({"erro": "Não foi possível acessar a API Helpdesk"}), 502)
    return response, None


def _json_response(response):
    try:
        body = response.json()
    except requests.exceptions.JSONDecodeError:
        body = {"erro": "A API Helpdesk retornou uma resposta inválida"}
    return jsonify(body), response.status_code


def _s3_target(url):
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    is_service_alias = hostname == "localstack"
    is_localstack = hostname == "localhost" or hostname.endswith(
        ".localhost.localstack.cloud"
    )
    is_loopback = hostname in {"127.0.0.1", "::1"}
    is_aws_s3 = hostname.endswith(".amazonaws.com") or hostname.endswith(
        ".amazonaws.com.cn"
    )
    if parsed.scheme not in {"http", "https"} or not (
        is_service_alias or is_localstack or is_loopback or is_aws_s3
    ):
        raise ValueError("destino de upload/download não permitido")

    headers = {}
    if is_service_alias or hostname.endswith(".localhost.localstack.cloud"):
        endpoint = urlsplit(current_app.config["LOCALSTACK_ENDPOINT"])
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            raise ValueError("LOCALSTACK_ENDPOINT inválido")
        target = urlunsplit(
            (parsed.scheme, endpoint.netloc, parsed.path, parsed.query, parsed.fragment)
        )
        if not is_service_alias:
            headers["Host"] = parsed.netloc
        return target, headers
    return url, headers


def create_app(api_url=None, localstack_endpoint=None, state_file=STATE_FILE):
    app = Flask(__name__)
    app.config.update(
        TEMPLATES_AUTO_RELOAD=True,
        SEND_FILE_MAX_AGE_DEFAULT=0,
        API_BASE=_validated_http_url(
            api_url if api_url is not None else _read_api_url(state_file)
        ),
        LOCALSTACK_ENDPOINT=(
            localstack_endpoint
            or os.environ.get("LOCALSTACK_ENDPOINT")
            or "http://localhost:4566"
        ),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 1024 * 1024,
    )

    @app.get("/")
    def index():
        return render_template("index.html", api_ready=bool(app.config["API_BASE"]))

    @app.get("/api/config")
    def config():
        return jsonify({"apiConfigured": bool(current_app.config["API_BASE"])})

    @app.get("/api/chamados")
    def list_tickets():
        params = {
            key: value
            for key, value in request.args.items()
            if key in {"status", "responsavel", "limit", "nextToken"} and value
        }
        response, error = _api_call("GET", "chamados", params=params)
        return error or _json_response(response)

    @app.post("/api/chamados")
    def create_ticket():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"erro": "Envie os dados do chamado em JSON"}), 400
        response, error = _api_call("POST", "chamados", json=payload)
        return error or _json_response(response)

    @app.get("/api/chamados/<chamado_id>")
    def get_ticket(chamado_id):
        response, error = _api_call(
            "GET", f"chamados/{quote(chamado_id, safe='')}"
        )
        return error or _json_response(response)

    @app.patch("/api/chamados/<chamado_id>")
    def update_ticket(chamado_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"erro": "Envie as alterações em JSON"}), 400
        response, error = _api_call(
            "PATCH", f"chamados/{quote(chamado_id, safe='')}", json=payload
        )
        return error or _json_response(response)

    @app.post("/api/chamados/<chamado_id>/comentarios")
    def add_comment(chamado_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"erro": "Envie o comentário em JSON"}), 400
        response, error = _api_call(
            "POST",
            f"chamados/{quote(chamado_id, safe='')}/comentarios",
            json=payload,
        )
        return error or _json_response(response)

    @app.post("/api/chamados/<chamado_id>/anexos")
    def upload_attachment(chamado_id):
        uploaded_file = request.files.get("arquivo")
        if uploaded_file is None or not uploaded_file.filename:
            return jsonify({"erro": "Selecione um arquivo"}), 400

        content_type = (uploaded_file.mimetype or "").lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            return jsonify({"erro": "Tipo de arquivo não permitido"}), 400

        file_content = uploaded_file.read(MAX_UPLOAD_BYTES + 1)
        if not file_content or len(file_content) > MAX_UPLOAD_BYTES:
            return jsonify({"erro": "Arquivo vazio ou maior que 10 MiB"}), 400

        path = f"chamados/{quote(chamado_id, safe='')}/anexos"
        response, error = _api_call(
            "POST",
            path,
            json={
                "nomeArquivo": uploaded_file.filename,
                "contentType": content_type,
                "tamanho": len(file_content),
            },
        )
        if error:
            return error
        if response.status_code != 201:
            return _json_response(response)

        try:
            upload_info = response.json()
            target, headers = _s3_target(upload_info["uploadUrl"])
            s3_response = requests.post(
                target,
                data=upload_info["fields"],
                files={
                    "file": (
                        uploaded_file.filename,
                        io.BytesIO(file_content),
                        content_type,
                    )
                },
                headers=headers,
                timeout=30,
            )
        except (KeyError, TypeError, ValueError, requests.RequestException):
            current_app.logger.warning("Falha no upload de anexo ao S3")
            return jsonify(
                {"erro": "Não foi possível enviar o anexo ao armazenamento"}
            ), 502

        if s3_response.status_code not in {200, 201, 204}:
            current_app.logger.warning(
                "S3 rejeitou o upload de anexo (HTTP %s)", s3_response.status_code
            )
            return jsonify(
                {"erro": "O armazenamento rejeitou o upload", "anexoId": upload_info.get("anexoId")}
            ), 502

        response, error = _api_call(
            "POST",
            f"{path}/confirmacao",
            json={"anexoId": upload_info["anexoId"]},
        )
        if error:
            return error
        return _json_response(response)

    @app.get("/api/chamados/<chamado_id>/anexos/<anexo_id>/arquivo")
    def download_attachment(chamado_id, anexo_id):
        path = (
            f"chamados/{quote(chamado_id, safe='')}/anexos/"
            f"{quote(anexo_id, safe='')}"
        )
        response, error = _api_call("GET", path)
        if error:
            return error
        if response.status_code != 200:
            return _json_response(response)

        try:
            download_info = response.json()
            target, headers = _s3_target(download_info["url"])
            file_response = requests.get(target, headers=headers, timeout=30)
        except (KeyError, TypeError, ValueError, requests.RequestException):
            current_app.logger.warning("Falha no download de anexo do S3")
            return jsonify({"erro": "Não foi possível baixar o anexo"}), 502

        if file_response.status_code != 200:
            current_app.logger.warning(
                "S3 rejeitou o download de anexo (HTTP %s)",
                file_response.status_code,
            )
            return jsonify({"erro": "O armazenamento rejeitou o download"}), 502

        return send_file(
            io.BytesIO(file_response.content),
            mimetype=file_response.headers.get("Content-Type", "application/octet-stream"),
            as_attachment=True,
            download_name=f"anexo-{anexo_id}",
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
