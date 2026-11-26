import uuid

from botocore.exceptions import ClientError

from comum import (
    agora,
    chamado_por_id,
    codigo_erro,
    criar_evento_outbox,
    ler_body,
    resposta,
    responsavel_do_evento,
    tabela,
    validar_texto,
    validar_uuid,
)


STATUS_VALIDOS = {"ABERTO", "EM_ATENDIMENTO", "RESOLVIDO", "FECHADO"}
PRIORIDADES_VALIDAS = {"BAIXA", "MEDIA", "ALTA", "CRITICA"}


def _gravar_atualizacao(
    tabela_chamados,
    chave,
    expressao,
    nomes,
    valores,
    evento,
    outbox=None,
    condicao_prioridade=None,
):
    atualizacao = {
        "TableName": tabela_chamados.name,
        "Key": chave,
        "UpdateExpression": expressao,
        "ExpressionAttributeNames": nomes,
        "ConditionExpression": "attribute_exists(PK)",
    }
    if condicao_prioridade:
        atualizacao["ConditionExpression"] += f" AND {condicao_prioridade}"
    if valores:
        atualizacao["ExpressionAttributeValues"] = valores

    transacoes = [
        {"Update": atualizacao},
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
    if outbox:
        transacoes.append(
            {
                "Put": {
                    "TableName": tabela_chamados.name,
                    "Item": outbox,
                    "ConditionExpression": (
                        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                    ),
                }
            }
        )
    tabela_chamados.meta.client.transact_write_items(
        TransactItems=transacoes
    )


def handler(event, context):
    try:
        chamado_id = validar_uuid(
            (event.get("pathParameters") or {}).get("id"),
            "id do chamado",
        )
        dados = ler_body(event)
        campos_permitidos = {"status", "prioridade", "responsavelId"}
        campos_invalidos = set(dados) - campos_permitidos
        if campos_invalidos:
            raise ValueError(f"campos não permitidos: {', '.join(sorted(campos_invalidos))}")
        if not dados:
            raise ValueError("informe ao menos um campo para atualizar")
    except ValueError as exc:
        return resposta(400, {"erro": str(exc)})

    nomes = {}
    valores = {}
    definicoes_set = []
    definicoes_remove = []
    status = ""
    prioridade = ""
    responsavel = None

    if "status" in dados:
        try:
            status = validar_texto(dados["status"], "status", 32).upper()
        except ValueError as exc:
            return resposta(400, {"erro": str(exc)})
        if status not in STATUS_VALIDOS:
            return resposta(400, {"erro": "status inválido"})
        nomes["#status"] = "status"
        nomes["#statusPk"] = "statusPk"
        nomes["#statusListaPk"] = "statusListaPk"
        valores[":status"] = status
        definicoes_set.append("#status = :status")
        definicoes_set.extend(
            ["#statusPk = :statusPk", "#statusListaPk = :statusListaPk"]
        )

    if "prioridade" in dados:
        try:
            prioridade = validar_texto(dados["prioridade"], "prioridade", 16).upper()
        except ValueError as exc:
            return resposta(400, {"erro": str(exc)})
        if prioridade not in PRIORIDADES_VALIDAS:
            return resposta(400, {"erro": "prioridade inválida"})
        nomes["#prioridade"] = "prioridade"
        valores[":prioridade"] = prioridade
        definicoes_set.append("#prioridade = :prioridade")

    if "responsavelId" in dados:
        responsavel = dados["responsavelId"]
        if responsavel is None:
            nomes["#responsavelId"] = "responsavelId"
            nomes["#responsavelPk"] = "responsavelPk"
            definicoes_remove.extend(["#responsavelId", "#responsavelPk"])
        else:
            try:
                responsavel = validar_texto(responsavel, "responsavelId", 128)
            except ValueError as exc:
                return resposta(400, {"erro": str(exc)})
            nomes["#responsavelId"] = "responsavelId"
            nomes["#responsavelPk"] = "responsavelPk"
            valores[":responsavelId"] = responsavel
            valores[":responsavelPk"] = f"RESPONSAVEL#{responsavel}"
            definicoes_set.extend(
                ["#responsavelId = :responsavelId", "#responsavelPk = :responsavelPk"]
            )

    tabela_chamados = tabela()
    atual = chamado_por_id(tabela_chamados, chamado_id)
    if not atual:
        return resposta(404, {"erro": "chamado não encontrado"})

    atualizado_em = agora()
    alteracoes = {}
    atualizado = dict(atual)

    if "status" in dados:
        criado_em = atual.get("criadoEm")
        if not isinstance(criado_em, str) or len(criado_em) < 7:
            raise ValueError("chamado sem data de criação válida")
        valores[":statusPk"] = f"{status}#{criado_em[:7]}"
        valores[":statusListaPk"] = f"STATUS#{status}"
        alteracoes["status"] = {"de": atual.get("status"), "para": status}
        atualizado.update(
            {
                "status": status,
                "statusPk": valores[":statusPk"],
                "statusListaPk": valores[":statusListaPk"],
            }
        )

    if "prioridade" in dados:
        alteracoes["prioridade"] = {
            "de": atual.get("prioridade"),
            "para": prioridade,
        }
        atualizado["prioridade"] = prioridade

    if "responsavelId" in dados:
        alteracoes["responsavelId"] = {
            "de": atual.get("responsavelId"),
            "para": responsavel,
        }
        if responsavel is None:
            atualizado.pop("responsavelId", None)
            atualizado.pop("responsavelPk", None)
        else:
            atualizado["responsavelId"] = responsavel
            atualizado["responsavelPk"] = f"RESPONSAVEL#{responsavel}"

    nomes["#atualizadoEm"] = "atualizadoEm"
    valores[":atualizadoEm"] = atualizado_em
    definicoes_set.append("#atualizadoEm = :atualizadoEm")
    expressao = "SET " + ", ".join(definicoes_set)
    if definicoes_remove:
        expressao += " REMOVE " + ", ".join(definicoes_remove)
    atualizado["atualizadoEm"] = atualizado_em

    evento = {
        "PK": f"CHAMADO#{chamado_id}",
        "SK": f"EVENTO#{atualizado_em}#{uuid.uuid4()}",
        "tipo": "CHAMADO_ATUALIZADO",
        "autorId": responsavel_do_evento(event),
        "alteracoes": alteracoes,
        "criadoEm": atualizado_em,
    }
    outbox = None
    condicao_prioridade = None
    if (
        "prioridade" in dados
        and prioridade == "CRITICA"
        and atual.get("prioridade") != "CRITICA"
    ):
        outbox = criar_evento_outbox(
            "chamado-critico",
            chamado_id,
            atualizado_em,
            prioridade="CRITICA",
        )
        nomes["#prioridade"] = "prioridade"
        prioridade_anterior = atual.get("prioridade")
        if prioridade_anterior is None:
            condicao_prioridade = "attribute_not_exists(#prioridade)"
        else:
            valores[":prioridadeAnterior"] = prioridade_anterior
            condicao_prioridade = "#prioridade = :prioridadeAnterior"

    try:
        _gravar_atualizacao(
            tabela_chamados,
            {"PK": f"CHAMADO#{chamado_id}", "SK": "META"},
            expressao,
            nomes,
            valores,
            evento,
            outbox,
            condicao_prioridade,
        )
    except ClientError as exc:
        if codigo_erro(exc) == "TransactionCanceledException":
            if not chamado_por_id(tabela_chamados, chamado_id):
                return resposta(404, {"erro": "chamado não encontrado"})
        raise

    return resposta(200, atualizado)
