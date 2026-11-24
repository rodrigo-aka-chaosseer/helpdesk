import base64
import binascii
import json

from boto3.dynamodb.conditions import Attr, Key

from comum import resposta, tabela, validar_texto


STATUS_VALIDOS = {"ABERTO", "EM_ATENDIMENTO", "RESOLVIDO", "FECHADO"}
LIMITE_PADRAO = 20
LIMITE_MAXIMO = 100


def _decodificar_token(token, filtros):
    if not token:
        return None
    if not isinstance(token, str) or len(token) > 4096:
        raise ValueError("nextToken inválido")
    try:
        dados = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if (
            not isinstance(dados, dict)
            or dados.get("filtros") != filtros
            or not isinstance(dados.get("cursor"), dict)
        ):
            raise ValueError
        return dados["cursor"]
    except (
        ValueError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("nextToken inválido ou incompatível com os filtros") from exc


def _codificar_token(cursor, filtros):
    dados = json.dumps({"cursor": cursor, "filtros": filtros}, separators=(",", ":"))
    return base64.urlsafe_b64encode(dados.encode()).decode().rstrip("=")


def handler(event, context):
    parametros = event.get("queryStringParameters") or {}
    try:
        status = parametros.get("status")
        if status is not None:
            status = validar_texto(status, "status", 32).upper()
            if status not in STATUS_VALIDOS:
                raise ValueError("status inválido")

        responsavel = parametros.get("responsavel")
        if responsavel is not None:
            responsavel = validar_texto(responsavel, "responsavel", 128)

        limite_texto = parametros.get("limit", str(LIMITE_PADRAO))
        if not isinstance(limite_texto, str) or not limite_texto.isdigit():
            raise ValueError("limit deve ser um inteiro positivo")
        limite = int(limite_texto)
        if not 1 <= limite <= LIMITE_MAXIMO:
            raise ValueError(f"limit deve estar entre 1 e {LIMITE_MAXIMO}")

        filtros = {"status": status, "responsavel": responsavel}
        cursor = _decodificar_token(parametros.get("nextToken"), filtros)
    except ValueError as exc:
        return resposta(400, {"erro": str(exc)})

    if status is not None:
        index_name = "GSI-StatusLista"
        key_condition = Key("statusListaPk").eq(f"STATUS#{status}")
    elif responsavel is not None:
        index_name = "GSI-Responsavel"
        key_condition = Key("responsavelPk").eq(f"RESPONSAVEL#{responsavel}")
    else:
        index_name = "GSI-Criado"
        key_condition = Key("tipoPk").eq("CHAMADO")

    if cursor:
        chave_indice = {
            "GSI-StatusLista": "statusListaPk",
            "GSI-Responsavel": "responsavelPk",
            "GSI-Criado": "tipoPk",
        }[index_name]
        if set(cursor) != {"PK", "SK", "criadoEmId", chave_indice} or any(
            not isinstance(valor, str) for valor in cursor.values()
        ):
            return resposta(400, {"erro": "nextToken inválido"})

    itens = []
    ultima_chave = None
    while len(itens) < limite:
        argumentos = {
            "IndexName": index_name,
            "KeyConditionExpression": key_condition,
            "Limit": limite - len(itens),
            "ScanIndexForward": False,
        }
        if responsavel is not None and status is not None:
            argumentos["FilterExpression"] = Attr("responsavelId").eq(responsavel)
        if cursor:
            argumentos["ExclusiveStartKey"] = cursor

        resultado = tabela().query(**argumentos)
        itens.extend(resultado.get("Items", []))
        cursor = resultado.get("LastEvaluatedKey")
        ultima_chave = cursor
        if not cursor:
            break

    next_token = _codificar_token(ultima_chave, filtros) if ultima_chave else None
    return resposta(200, {"items": itens[:limite], "nextToken": next_token})
