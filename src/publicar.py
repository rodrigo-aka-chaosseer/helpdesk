import json
import os

from boto3.dynamodb.types import TypeDeserializer

from comum import cliente_sns


_DYNAMODB_DESERIALIZER = TypeDeserializer()


def _desserializar_imagem(imagem):
    return {
        nome: _DYNAMODB_DESERIALIZER.deserialize(valor)
        for nome, valor in imagem.items()
    }


def handler(event, _context):
    cliente = cliente_sns()
    topicos = {
        "chamado-criado": os.environ.get("TOPICO_CHAMADO_CRIADO"),
        "chamado-critico": os.environ.get("TOPICO_CHAMADO_CRITICO"),
    }

    for registro in event.get("Records", []):
        if registro.get("eventName") != "INSERT":
            continue
        imagem = (registro.get("dynamodb") or {}).get("NewImage")
        if not imagem:
            continue

        item = _desserializar_imagem(imagem)
        if item.get("entityType") != "OUTBOX":
            continue

        tipo = item["eventType"]
        arn_topico = topicos.get(tipo)
        if not arn_topico:
            raise RuntimeError(f"tópico SNS não configurado para o evento {tipo}")

        mensagem = {
            "eventId": item["eventId"],
            "eventType": tipo,
            "chamadoId": item["chamadoId"],
            "timestamp": item["timestamp"],
        }
        if "prioridade" in item:
            mensagem["prioridade"] = item["prioridade"]

        cliente.publish(
            TopicArn=arn_topico,
            Message=json.dumps(mensagem, ensure_ascii=False),
        )

    return {"publicados": True}
