from boto3.dynamodb.conditions import Key

from comum import resposta, tabela, validar_uuid


def handler(event, context):
    try:
        chamado_id = validar_uuid(
            (event.get("pathParameters") or {}).get("id"),
            "id do chamado",
        )
    except ValueError as exc:
        return resposta(400, {"erro": str(exc)})

    tabela_chamados = tabela()
    resultado = tabela_chamados.query(
        KeyConditionExpression=Key("PK").eq(f"CHAMADO#{chamado_id}"),
        ConsistentRead=True,
    )
    itens = resultado.get("Items", [])
    while resultado.get("LastEvaluatedKey"):
        resultado = tabela_chamados.query(
            KeyConditionExpression=Key("PK").eq(f"CHAMADO#{chamado_id}"),
            ExclusiveStartKey=resultado["LastEvaluatedKey"],
            ConsistentRead=True,
        )
        itens.extend(resultado.get("Items", []))

    chamado = next((item for item in itens if item.get("SK") == "META"), None)
    if chamado is None:
        return resposta(404, {"erro": "chamado não encontrado"})

    historico = [item for item in itens if item.get("SK") != "META"]
    historico.sort(key=lambda item: (item.get("criadoEm", ""), item.get("SK", "")))
    return resposta(200, {**chamado, "historico": historico})
