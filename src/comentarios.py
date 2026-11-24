import uuid

from comum import (
    agora,
    chamado_por_id,
    ler_body,
    resposta,
    responsavel_do_evento,
    tabela,
    validar_texto,
    validar_uuid,
)


def handler(event, context):
    try:
        chamado_id = validar_uuid(
            (event.get("pathParameters") or {}).get("id"),
            "id do chamado",
        )
        dados = ler_body(event)
        texto = validar_texto(dados.get("texto"), "texto", 5000)
        if set(dados) - {"texto"}:
            raise ValueError("somente o campo texto é permitido")
    except ValueError as exc:
        return resposta(400, {"erro": str(exc)})

    tabela_chamados = tabela()
    if not chamado_por_id(tabela_chamados, chamado_id):
        return resposta(404, {"erro": "chamado não encontrado"})

    criado_em = agora()
    comentario = {
        "PK": f"CHAMADO#{chamado_id}",
        "SK": f"EVENTO#{criado_em}#{uuid.uuid4()}",
        "autorId": responsavel_do_evento(event),
        "tipo": "COMENTARIO",
        "texto": texto,
        "criadoEm": criado_em,
    }
    tabela_chamados.put_item(
        Item=comentario,
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )
    return resposta(201, comentario)
