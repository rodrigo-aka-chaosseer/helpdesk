import os
import re
import time
import uuid

from botocore.exceptions import ClientError

from comum import (
    agora,
    chamado_por_id,
    cliente_s3,
    codigo_erro,
    ler_body,
    resposta,
    tabela,
    validar_texto,
    validar_uuid,
)


EXPIRACAO_URL_SEGUNDOS = 300
MAX_ANEXO_BYTES_PADRAO = 10 * 1024 * 1024
TIPOS_ANEXO_PADRAO = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "text/plain",
}


def _configuracao_anexos():
    try:
        max_bytes = int(os.environ.get("MAX_ANEXO_BYTES", MAX_ANEXO_BYTES_PADRAO))
    except ValueError as exc:
        raise RuntimeError("MAX_ANEXO_BYTES deve ser um inteiro") from exc
    if max_bytes <= 0:
        raise RuntimeError("MAX_ANEXO_BYTES deve ser positivo")
    tipos = os.environ.get("TIPOS_ANEXO_PERMITIDOS")
    permitidos = (
        {tipo.strip().lower() for tipo in tipos.split(",") if tipo.strip()}
        if tipos
        else TIPOS_ANEXO_PADRAO
    )
    if not permitidos:
        raise RuntimeError("TIPOS_ANEXO_PERMITIDOS não pode ser vazio")
    return max_bytes, permitidos


def _nome_seguro(nome):
    base = nome.replace("\\", "/").split("/")[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")


def _obter_item_anexo(tabela_chamados, chamado_id, anexo_id):
    resultado = tabela_chamados.get_item(
        Key={"PK": f"CHAMADO#{chamado_id}", "SK": f"ANEXO#{anexo_id}"}
    )
    return resultado.get("Item")


def upload_handler(event, context):
    try:
        chamado_id = validar_uuid(
            (event.get("pathParameters") or {}).get("id"),
            "id do chamado",
        )
        dados = ler_body(event)
        if set(dados) != {"nomeArquivo", "contentType", "tamanho"}:
            raise ValueError("informe nomeArquivo, contentType e tamanho")
        nome_original = validar_texto(dados["nomeArquivo"], "nomeArquivo", 255)
        content_type = validar_texto(dados["contentType"], "contentType", 128).lower()
        tamanho = dados["tamanho"]
        if isinstance(tamanho, bool) or not isinstance(tamanho, int):
            raise ValueError("tamanho deve ser um inteiro")
        nome_seguro = _nome_seguro(nome_original)
        if not nome_seguro:
            raise ValueError("nomeArquivo inválido")
    except (ValueError, TypeError) as exc:
        return resposta(400, {"erro": str(exc)})

    max_bytes, tipos_permitidos = _configuracao_anexos()
    if not 0 < tamanho <= max_bytes:
        return resposta(400, {"erro": f"tamanho deve estar entre 1 e {max_bytes} bytes"})
    if content_type not in tipos_permitidos:
        return resposta(400, {"erro": "tipo de arquivo não permitido"})

    tabela_chamados = tabela()
    if not chamado_por_id(tabela_chamados, chamado_id):
        return resposta(404, {"erro": "chamado não encontrado"})

    anexo_id = str(uuid.uuid4())
    chave_s3 = f"anexos/{chamado_id}/{anexo_id}/{nome_seguro}"
    criado_em = agora()
    item = {
        "PK": f"CHAMADO#{chamado_id}",
        "SK": f"ANEXO#{anexo_id}",
        "anexoId": anexo_id,
        "nomeArquivo": nome_original,
        "chaveS3": chave_s3,
        "tamanho": tamanho,
        "contentType": content_type,
        "status": "PENDENTE",
        "criadoEm": criado_em,
        "ttl": int(time.time()) + 7 * 24 * 60 * 60,
    }
    url = cliente_s3().generate_presigned_post(
        Bucket=os.environ["BUCKET_ANEXOS"],
        Key=chave_s3,
        Fields={
            "Content-Type": content_type,
            "tagging": "status=pendente",
        },
        Conditions=[
            {"Content-Type": content_type},
            {"tagging": "status=pendente"},
            ["content-length-range", tamanho, tamanho],
        ],
        ExpiresIn=EXPIRACAO_URL_SEGUNDOS,
    )
    tabela_chamados.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )
    return resposta(
        201,
        {
            "anexoId": anexo_id,
            "uploadUrl": url["url"],
            "fields": url["fields"],
            "expiresIn": EXPIRACAO_URL_SEGUNDOS,
        },
    )


def confirmar_handler(event, context):
    try:
        chamado_id = validar_uuid(
            (event.get("pathParameters") or {}).get("id"),
            "id do chamado",
        )
        dados = ler_body(event)
        if set(dados) != {"anexoId"}:
            raise ValueError("informe somente anexoId")
        anexo_id = validar_uuid(dados["anexoId"], "anexoId")
    except ValueError as exc:
        return resposta(400, {"erro": str(exc)})

    tabela_chamados = tabela()
    if not chamado_por_id(tabela_chamados, chamado_id):
        return resposta(404, {"erro": "chamado não encontrado"})
    anexo = _obter_item_anexo(tabela_chamados, chamado_id, anexo_id)
    if not anexo:
        return resposta(404, {"erro": "anexo não encontrado"})
    if anexo["status"] == "CONFIRMADO":
        return resposta(200, {"anexoId": anexo_id, "status": "CONFIRMADO"})
    if anexo["status"] != "PENDENTE":
        return resposta(409, {"erro": "anexo não está pendente"})

    bucket = os.environ["BUCKET_ANEXOS"]
    s3 = cliente_s3()
    try:
        objeto = s3.head_object(Bucket=bucket, Key=anexo["chaveS3"])
        version_id = objeto.get("VersionId")
        if not version_id:
            return resposta(409, {"erro": "versão do arquivo não identificada"})
        tags = s3.get_object_tagging(
            Bucket=bucket,
            Key=anexo["chaveS3"],
            VersionId=version_id,
        ).get("TagSet", [])
    except ClientError as exc:
        if codigo_erro(exc) in {"404", "NoSuchKey", "NotFound"}:
            return resposta(409, {"erro": "o arquivo ainda não foi enviado ao S3"})
        raise

    if objeto.get("ContentLength") != anexo["tamanho"]:
        return resposta(409, {"erro": "tamanho do arquivo não corresponde ao solicitado"})
    if objeto.get("ContentType") != anexo["contentType"]:
        return resposta(409, {"erro": "tipo do arquivo não corresponde ao solicitado"})
    status_tag = next((tag["Value"] for tag in tags if tag["Key"] == "status"), None)
    if status_tag not in {"pendente", "confirmado"}:
        return resposta(409, {"erro": "tag de estado do arquivo inválida"})

    if status_tag == "pendente":
        tag_set = [tag for tag in tags if tag["Key"] != "status"]
        tag_set.append({"Key": "status", "Value": "confirmado"})
        s3.put_object_tagging(
            Bucket=bucket,
            Key=anexo["chaveS3"],
            VersionId=version_id,
            Tagging={"TagSet": tag_set},
        )

    try:
        resultado = tabela_chamados.update_item(
            Key={"PK": f"CHAMADO#{chamado_id}", "SK": f"ANEXO#{anexo_id}"},
            UpdateExpression=(
                "SET #status = :confirmado, confirmadoEm = :agora, "
                "#versionId = :versionId REMOVE #ttl"
            ),
            ConditionExpression="#status = :pendente",
            ExpressionAttributeNames={
                "#status": "status",
                "#ttl": "ttl",
                "#versionId": "versionId",
            },
            ExpressionAttributeValues={
                ":confirmado": "CONFIRMADO",
                ":pendente": "PENDENTE",
                ":agora": agora(),
                ":versionId": version_id,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if codigo_erro(exc) != "ConditionalCheckFailedException":
            raise
        atualizado = _obter_item_anexo(tabela_chamados, chamado_id, anexo_id)
        if not atualizado or atualizado.get("status") != "CONFIRMADO":
            raise
        return resposta(200, {"anexoId": anexo_id, "status": "CONFIRMADO"})

    return resposta(
        200,
        {
            "anexoId": anexo_id,
            "status": resultado["Attributes"]["status"],
        },
    )


def download_handler(event, context):
    try:
        path = event.get("pathParameters") or {}
        chamado_id = validar_uuid(path.get("id"), "id do chamado")
        anexo_id = validar_uuid(path.get("anexoId"), "anexoId")
    except ValueError as exc:
        return resposta(400, {"erro": str(exc)})

    tabela_chamados = tabela()
    if not chamado_por_id(tabela_chamados, chamado_id):
        return resposta(404, {"erro": "chamado não encontrado"})
    anexo = _obter_item_anexo(tabela_chamados, chamado_id, anexo_id)
    if not anexo or anexo.get("status") != "CONFIRMADO":
        return resposta(404, {"erro": "anexo não encontrado"})
    if not anexo.get("versionId"):
        return resposta(409, {"erro": "versão confirmada do anexo não registrada"})

    url = cliente_s3().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": os.environ["BUCKET_ANEXOS"],
            "Key": anexo["chaveS3"],
            "VersionId": anexo["versionId"],
        },
        ExpiresIn=EXPIRACAO_URL_SEGUNDOS,
    )
    return resposta(
        200,
        {"anexoId": anexo_id, "url": url, "expiresIn": EXPIRACAO_URL_SEGUNDOS},
    )
