import json
import os
import uuid

from botocore.exceptions import ClientError

from comum import agora, codigo_erro, tabela, validar_texto, validar_uuid


def _chamado_id_da_mensagem(registro):
    corpo = registro.get("body")
    if not isinstance(corpo, str):
        raise ValueError("corpo da mensagem SQS inválido")
    try:
        mensagem = json.loads(corpo)
    except json.JSONDecodeError as exc:
        raise ValueError("JSON inválido na mensagem SQS") from exc
    if not isinstance(mensagem, dict):
        raise ValueError("a mensagem SQS deve conter um objeto JSON")
    if mensagem.get("eventType") != "chamado-criado":
        raise ValueError("tipo de evento SQS não suportado")
    return validar_uuid(mensagem.get("chamadoId"), "id do chamado")


def _registrar_atribuicao(tabela_chamados, chamado, atendente_id, timestamp):
    chamado_id = chamado["id"]
    criado_em = chamado.get("criadoEm")
    if not isinstance(criado_em, str) or len(criado_em) < 7:
        raise ValueError("chamado sem data de criação válida")

    chave = {"PK": f"CHAMADO#{chamado_id}", "SK": "META"}
    nomes = {
        "#responsavelId": "responsavelId",
        "#responsavelPk": "responsavelPk",
        "#status": "status",
        "#statusPk": "statusPk",
        "#statusListaPk": "statusListaPk",
        "#atualizadoEm": "atualizadoEm",
    }
    valores = {
        ":responsavelId": atendente_id,
        ":responsavelPk": f"RESPONSAVEL#{atendente_id}",
        ":aberto": "ABERTO",
        ":emAtendimento": "EM_ATENDIMENTO",
        ":statusPk": f"EM_ATENDIMENTO#{criado_em[:7]}",
        ":statusListaPk": "STATUS#EM_ATENDIMENTO",
        ":atualizadoEm": timestamp,
    }
    evento = {
        "PK": chave["PK"],
        "SK": f"EVENTO#{timestamp}#{uuid.uuid4()}",
        "tipo": "CHAMADO_ATRIBUIDO_AUTOMATICAMENTE",
        "autorId": "sistema",
        "responsavelId": atendente_id,
        "alteracoes": {
            "responsavelId": {
                "de": None,
                "para": atendente_id,
            },
            "status": {
                "de": "ABERTO",
                "para": "EM_ATENDIMENTO",
            },
        },
        "criadoEm": timestamp,
    }

    try:
        tabela_chamados.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": tabela_chamados.name,
                        "Key": chave,
                        "UpdateExpression": (
                            "SET #responsavelId = :responsavelId, "
                            "#responsavelPk = :responsavelPk, "
                            "#status = :emAtendimento, "
                            "#statusPk = :statusPk, "
                            "#statusListaPk = :statusListaPk, "
                            "#atualizadoEm = :atualizadoEm"
                        ),
                        "ConditionExpression": (
                            "attribute_exists(PK) "
                            "AND attribute_not_exists(#responsavelId) "
                            "AND #status = :aberto"
                        ),
                        "ExpressionAttributeNames": nomes,
                        "ExpressionAttributeValues": valores,
                    }
                },
                {
                    "Put": {
                        "TableName": tabela_chamados.name,
                        "Item": evento,
                        "ConditionExpression": (
                            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                        ),
                    }
                },
            ]
        )
    except ClientError as exc:
        if codigo_erro(exc) != "TransactionCanceledException":
            raise

        atual = tabela_chamados.get_item(Key=chave, ConsistentRead=True).get("Item")
        if atual is None:
            raise RuntimeError("chamado desapareceu durante a atribuição") from exc
        if "responsavelId" in atual or atual.get("status") != "ABERTO":
            return False
        raise

    return True


def handler(event, _context):
    registros = event.get("Records", [])
    if not isinstance(registros, list):
        raise ValueError("evento SQS inválido")
    if not registros:
        return {"processados": 0}

    atendente_id = validar_texto(
        os.environ.get("ATENDENTE_DEMO_ID"),
        "ATENDENTE_DEMO_ID",
        128,
    )
    tabela_chamados = tabela()
    processados = 0

    for registro in registros:
        chamado_id = _chamado_id_da_mensagem(registro)
        chave = {"PK": f"CHAMADO#{chamado_id}", "SK": "META"}
        chamado = tabela_chamados.get_item(
            Key=chave,
            ConsistentRead=True,
        ).get("Item")
        if chamado is None:
            raise RuntimeError(f"chamado {chamado_id} não encontrado")
        if "responsavelId" in chamado or chamado.get("status") != "ABERTO":
            continue

        if _registrar_atribuicao(
            tabela_chamados,
            chamado,
            atendente_id,
            agora(),
        ):
            processados += 1

    return {"processados": processados}
