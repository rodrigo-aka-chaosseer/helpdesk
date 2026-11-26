import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config


def tabela():
    endpoint = os.environ.get("ENDPOINT_URL") or None
    return boto3.resource("dynamodb", endpoint_url=endpoint).Table(
        os.environ["TABELA"]
    )


def cliente_s3():
    endpoint = os.environ.get("ENDPOINT_URL") or None
    config = Config(s3={"addressing_style": "path"}) if endpoint else None
    return boto3.client("s3", endpoint_url=endpoint, config=config)


def cliente_sns():
    endpoint = os.environ.get("ENDPOINT_URL") or None
    return boto3.client("sns", endpoint_url=endpoint)


def criar_evento_outbox(tipo, chamado_id, timestamp, **atributos):
    evento_id = str(uuid.uuid4())
    item = {
        "PK": f"OUTBOX#{evento_id}",
        "SK": "EVENTO",
        "entityType": "OUTBOX",
        "eventId": evento_id,
        "eventType": tipo,
        "chamadoId": chamado_id,
        "timestamp": timestamp,
        "ttl": int(time.time()) + 30 * 24 * 60 * 60,
    }
    item.update(atributos)
    return item


def _json_default(valor):
    if isinstance(valor, Decimal):
        return int(valor) if valor % 1 == 0 else float(valor)
    if isinstance(valor, datetime):
        return valor.isoformat()
    raise TypeError(f"tipo não serializável: {type(valor).__name__}")


def resposta(status, corpo):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(corpo, default=_json_default, ensure_ascii=False),
    }


def ler_body(event):
    body = event.get("body")
    if body is None or body == "":
        return {}
    if isinstance(body, dict):
        return body
    if not isinstance(body, str):
        raise ValueError("corpo da requisição inválido")
    try:
        dados = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("JSON inválido") from exc
    if not isinstance(dados, dict):
        raise ValueError("o corpo da requisição deve ser um objeto JSON")
    return dados


def validar_uuid(valor, campo="id"):
    if not isinstance(valor, str):
        raise ValueError(f"{campo} inválido")
    try:
        return str(uuid.UUID(valor))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{campo} inválido") from exc


def validar_texto(valor, campo, maximo, obrigatorio=True):
    if not isinstance(valor, str):
        raise ValueError(f"{campo} deve ser texto")
    valor = valor.strip()
    if obrigatorio and not valor:
        raise ValueError(f"{campo} é obrigatório")
    if len(valor) > maximo:
        raise ValueError(f"{campo} excede o limite de {maximo} caracteres")
    if any(ord(caractere) < 32 and caractere not in "\t\n\r" for caractere in valor):
        raise ValueError(f"{campo} contém caracteres inválidos")
    return valor


def agora():
    return datetime.now(timezone.utc).isoformat()


def responsavel_do_evento(event):
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    claims = authorizer.get("claims") or (authorizer.get("jwt") or {}).get("claims") or {}
    identidade = claims.get("sub") or claims.get("username")
    return identidade if isinstance(identidade, str) and identidade else "anonimo"


def codigo_erro(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code")


def chamado_por_id(tabela_chamados, chamado_id):
    resultado = tabela_chamados.get_item(
        Key={"PK": f"CHAMADO#{chamado_id}", "SK": "META"}
    )
    return resultado.get("Item")
