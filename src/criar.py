import json, uuid
from datetime import datetime, timezone
from comum import tabela, resposta

def handler(event, context):
    dados = json.loads(event.get("body") or "{}")
    for campo in ("titulo", "descricao", "categoria"):
        if not dados.get(campo):
            return resposta(400, {"erro": f"campo obrigatório: {campo}"})

    agora = datetime.now(timezone.utc)
    chamado_id = str(uuid.uuid4())
    status = "ABERTO"
    item = {
        "PK": f"CHAMADO#{chamado_id}",
        "SK": "META",
        "id": chamado_id,
        "titulo": dados["titulo"],
        "descricao": dados["descricao"],
        "categoria": dados["categoria"],
        "prioridade": "MEDIA",
        "status": status,
        "statusPk": f"{status}#{agora:%Y-%m}",
        "criadoEm": agora.isoformat(),
    }
    tabela().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return resposta(201, {"id": chamado_id})