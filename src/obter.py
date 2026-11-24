from comum import tabela, resposta

def handler(event, context):
    chamado_id = event["pathParameters"]["id"]
    r = tabela().get_item(Key={"PK": f"CHAMADO#{chamado_id}", "SK": "META"})
    if "Item" not in r:
        return resposta(404, {"erro": "chamado não encontrado"})
    return resposta(200, r["Item"])