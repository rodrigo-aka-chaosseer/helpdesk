import uuid

from comum import (
    agora,
    criar_evento_outbox,
    ler_body,
    resposta,
    responsavel_do_evento,
    serializar_item,
    tabela,
    validar_texto,
)


def handler(event, context):
    try:
        dados = ler_body(event)
        titulo = validar_texto(dados.get("titulo"), "titulo", 200)
        descricao = validar_texto(dados.get("descricao"), "descricao", 5000)
        categoria = validar_texto(dados.get("categoria"), "categoria", 100)
    except ValueError as exc:
        return resposta(400, {"erro": str(exc)})

    criado_em = agora()
    chamado_id = str(uuid.uuid4())
    status = "ABERTO"
    item = {
        "PK": f"CHAMADO#{chamado_id}",
        "SK": "META",
        "id": chamado_id,
        "titulo": titulo,
        "descricao": descricao,
        "categoria": categoria,
        "prioridade": "MEDIA",
        "status": status,
        "statusPk": f"{status}#{criado_em[:7]}",
        "statusListaPk": f"STATUS#{status}",
        "tipoPk": "CHAMADO",
        "criadoEm": criado_em,
        "criadoEmId": f"{criado_em}#{chamado_id}",
    }

    identidade = responsavel_do_evento(event)
    if identidade != "anonimo":
        item["clienteId"] = identidade

    evento = criar_evento_outbox("chamado-criado", chamado_id, criado_em)
    tabela_chamados = tabela()
    tabela_chamados.meta.client.transact_write_items(
        TransactItems=[
            {
                "Put": {
                    "TableName": tabela_chamados.name,
                    "Item": serializar_item(item),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Put": {
                    "TableName": tabela_chamados.name,
                    "Item": serializar_item(evento),
                    "ConditionExpression": (
                        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                    ),
                }
            },
        ]
    )
    return resposta(201, {"id": chamado_id})
