import json
import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import Blueprint, current_app, jsonify

bp = Blueprint("mensageria", __name__)

FILAS = [
    "atribuicao",
    "atribuicao-dlq",
    "publicacao-eventos-dlq",
    "alertas-criticos-demo",
    "auditoria-demo",
]
MENSAGEM_INVALIDA = json.dumps(
    {"eventType": "chamado-criado", "chamadoId": "uuid-invalido"}
)


def _sqs():
    return boto3.client(
        "sqs",
        endpoint_url=current_app.config["LOCALSTACK_ENDPOINT"],
        region_name=os.environ.get("LOCALSTACK_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _url(sqs, nome):
    return sqs.get_queue_url(QueueName=nome)["QueueUrl"]

def _espiar(nome, maximo=10):
    sqs = _sqs()
    resposta = sqs.receive_message(
        QueueUrl=_url(sqs, nome),
        MaxNumberOfMessages=maximo,
        VisibilityTimeout=0,
        WaitTimeSeconds=0,
    )
    eventos = {}
    for mensagem in resposta.get("Messages", []):
        try:
            evento = json.loads(mensagem["Body"])
        except json.JSONDecodeError:
            continue
        eventos[evento.get("eventId", mensagem["MessageId"])] = evento
    return sorted(eventos.values(), key=lambda e: e.get("timestamp", ""), reverse=True)

@bp.get("/api/mensageria/filas")
def filas():
    sqs, saida = _sqs(), []
    for nome in FILAS:
        try:
            attrs = sqs.get_queue_attributes(
                QueueUrl=_url(sqs, nome),
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )["Attributes"]
            saida.append(
                {
                    "nome": nome,
                    "visiveis": int(attrs["ApproximateNumberOfMessages"]),
                    "emProcessamento": int(
                        attrs["ApproximateNumberOfMessagesNotVisible"]
                    ),
                }
            )
        except (ClientError, BotoCoreError):
            saida.append({"nome": nome, "erro": True})
    return jsonify(saida)


@bp.get("/api/mensageria/alertas")
def alertas():
    try:
        return jsonify(_espiar("alertas-criticos-demo"))
    except (ClientError, BotoCoreError):
        return jsonify({"erro": "Não foi possível ler a fila de alertas"}), 502

@bp.get("/api/mensageria/auditoria")
def auditoria():
    try:
        return jsonify(_espiar("auditoria-demo"))
    except (ClientError, BotoCoreError):
        return jsonify({"erro": "Não foi possível ler a fila de auditoria"}), 502

@bp.post("/api/mensageria/falha")
def simular_falha():
    try:
        sqs = _sqs()
        sqs.send_message(
            QueueUrl=_url(sqs, "atribuicao"), MessageBody=MENSAGEM_INVALIDA
        )
    except (ClientError, BotoCoreError):
        return jsonify({"erro": "Não foi possível enviar a mensagem"}), 502
    return jsonify({"enviada": True})


@bp.post("/api/mensageria/limpar")
def limpar():
    try:
        sqs = _sqs()
        for nome in ("atribuicao-dlq", "alertas-criticos-demo", "auditoria-demo"):
            sqs.purge_queue(QueueUrl=_url(sqs, nome))
    except (ClientError, BotoCoreError):
        return jsonify({"erro": "Não foi possível limpar as filas"}), 502
    return jsonify({"limpas": True})