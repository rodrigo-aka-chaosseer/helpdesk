import os, json, boto3

def tabela():
    endpoint = os.environ.get("ENDPOINT_URL") or None
    return boto3.resource("dynamodb", endpoint_url=endpoint).Table(os.environ["TABELA"])

def resposta(status, corpo):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(corpo, default=str),
    }