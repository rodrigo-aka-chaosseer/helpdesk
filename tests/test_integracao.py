import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import anexos
import atribuir
import atualizar
import comentarios
import criar
import listar
import obter
import publicar


CHAMADO_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ANEXO_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_DYNAMODB_SERIALIZER = TypeSerializer()
_DYNAMODB_DESERIALIZER = TypeDeserializer()


def deserialize_item(item):
    return {
        key: _DYNAMODB_DESERIALIZER.deserialize(value)
        for key, value in item.items()
    }


def serialize_item(item):
    return {
        key: _DYNAMODB_SERIALIZER.serialize(value)
        for key, value in item.items()
    }


def sqs_event(chamado_id):
    return {
        "Records": [
            {
                "body": json.dumps(
                    {
                        "eventId": "evento-demo",
                        "eventType": "chamado-criado",
                        "chamadoId": chamado_id,
                        "timestamp": "2026-01-01T00:00:00+00:00",
                    }
                )
            }
        ]
    }


def event(body=None, path=None, query=None, claims=None):
    request_context = {}
    if claims is not None:
        request_context["authorizer"] = {"claims": claims}
    return {
        "body": json.dumps(body) if isinstance(body, dict) else body,
        "pathParameters": path or {},
        "queryStringParameters": query or {},
        "requestContext": request_context,
    }


def body(response):
    return json.loads(response["body"])


class FakeDynamoClient:
    def __init__(self, table):
        self.table = table
        self.transaction_calls = []

    def transact_write_items(self, **kwargs):
        self.transaction_calls.append(kwargs)
        for transaction in kwargs["TransactItems"]:
            update = transaction.get("Update")
            if update:
                chave = deserialize_item(update["Key"])
                item = self.table.items.get((chave["PK"], chave["SK"]))
                if item:
                    nomes = update["ExpressionAttributeNames"]
                    valores = deserialize_item(
                        update.get("ExpressionAttributeValues", {})
                    )
                    expressao, *remocoes = update["UpdateExpression"].split(
                        " REMOVE ", 1
                    )
                    for atribuicao in expressao.removeprefix("SET ").split(","):
                        nome, valor = (
                            parte.strip() for parte in atribuicao.split("=", 1)
                        )
                        item[nomes[nome]] = valores[valor]
                    if remocoes:
                        for nome in remocoes[0].split(","):
                            item.pop(nomes[nome.strip()], None)
            put = transaction.get("Put")
            if put:
                item = deserialize_item(put["Item"])
                self.table.items[(item["PK"], item["SK"])] = item


class ConcurrentAssignmentClient:
    def __init__(self, table):
        self.table = table

    def transact_write_items(self, **kwargs):
        key = deserialize_item(kwargs["TransactItems"][0]["Update"]["Key"])
        item = self.table.items[(key["PK"], key["SK"])]
        item.update(
            {
                "responsavelId": "atendente-demo-01",
                "responsavelPk": "RESPONSAVEL#atendente-demo-01",
                "status": "EM_ATENDIMENTO",
            }
        )
        raise ClientError(
            {"Error": {"Code": "TransactionCanceledException"}},
            "TransactWriteItems",
        )


class FakeTable:
    def __init__(self, items=None, query_pages=None):
        self.name = "Chamados"
        self.items = {
            (item["PK"], item["SK"]): dict(item)
            for item in (items or [])
        }
        self.meta = SimpleNamespace(client=FakeDynamoClient(self))
        self.query_pages = list(query_pages or [])
        self.query_calls = []
        self.put_calls = []
        self.update_calls = []

    def get_item(self, Key, **kwargs):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, **kwargs):
        self.put_calls.append((Item, kwargs))
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        if self.query_pages:
            return self.query_pages.pop(0)
        expressao = kwargs.get("KeyConditionExpression")
        valores = getattr(expressao, "_values", ())
        if len(valores) == 2:
            nome_chave = valores[0].name
            valor_chave = valores[1]
        else:
            nome_chave = None
            valor_chave = None
        return {
            "Items": [
                dict(item)
                for (item_pk, _), item in self.items.items()
                if item_pk == valor_chave
                or (nome_chave and item.get(nome_chave) == valor_chave)
            ]
        }

    def update_item(self, **kwargs):
        self.update_calls.append(kwargs)
        key = kwargs["Key"]
        item = self.items.get((key["PK"], key["SK"]))
        if not item:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "UpdateItem",
            )
        values = kwargs.get("ExpressionAttributeValues", {})
        if ":confirmado" in values:
            item["status"] = values[":confirmado"]
            item["confirmadoEm"] = values[":agora"]
            item["versionId"] = values[":versionId"]
            item.pop("ttl", None)
        return {"Attributes": dict(item)}


class FakeS3:
    def __init__(self):
        self.presigned_posts = []
        self.presigned_urls = []
        self.head = {
            "ContentLength": 4,
            "ContentType": "text/plain",
            "VersionId": "version-1",
        }
        self.tags = [{"Key": "status", "Value": "pendente"}]
        self.tag_updates = []
        self.head_error = None

    def generate_presigned_post(self, **kwargs):
        self.presigned_posts.append(kwargs)
        return {"url": "https://s3.test/upload", "fields": {"key": kwargs["Key"]}}

    def generate_presigned_url(self, operation, **kwargs):
        self.presigned_urls.append((operation, kwargs))
        return "https://s3.test/download"

    def head_object(self, **kwargs):
        if self.head_error:
            raise self.head_error
        return self.head

    def get_object_tagging(self, **kwargs):
        return {"TagSet": list(self.tags)}

    def put_object_tagging(self, **kwargs):
        self.tag_updates.append(kwargs)
        self.tags = kwargs["Tagging"]["TagSet"]


class FakeSNS:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def publish(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append(kwargs)
        return {"MessageId": "message-1"}


class EndpointsTest(unittest.TestCase):
    def setUp(self):
        self.chamado = {
            "PK": f"CHAMADO#{CHAMADO_ID}",
            "SK": "META",
            "id": CHAMADO_ID,
            "titulo": "Falha",
            "status": "ABERTO",
            "criadoEm": "2026-01-01T00:00:00+00:00",
        }

    def test_criar_persiste_chamado_e_rejeita_json_invalido(self):
        table = FakeTable()
        with patch.object(criar, "tabela", return_value=table):
            response = criar.handler(
                event({"titulo": "Falha", "descricao": "Não inicia", "categoria": "App"}),
                None,
            )
            invalid = criar.handler(event(body="{"), None)

        self.assertEqual(response["statusCode"], 201)
        stored = next(iter(table.items.values()))
        self.assertEqual(stored["status"], "ABERTO")
        self.assertEqual(stored["tipoPk"], "CHAMADO")
        self.assertTrue(stored["criadoEmId"].endswith(f"#{stored['id']}"))
        outbox = next(item for item in table.items.values() if item.get("entityType") == "OUTBOX")
        self.assertEqual(outbox["eventType"], "chamado-criado")
        self.assertEqual(outbox["chamadoId"], stored["id"])
        self.assertEqual(invalid["statusCode"], 400)

    def test_obter_retorna_historico_paginado_e_404(self):
        table = FakeTable(
            query_pages=[
                {
                    "Items": [self.chamado],
                    "LastEvaluatedKey": {"PK": self.chamado["PK"], "SK": "META"},
                },
                {
                    "Items": [
                        {
                            "PK": self.chamado["PK"],
                            "SK": "EVENTO#2026-01-02T00:00:00Z#1",
                            "criadoEm": "2026-01-02T00:00:00Z",
                            "texto": "Acompanhe",
                        }
                    ]
                },
            ]
        )
        with patch.object(obter, "tabela", return_value=table):
            response = obter.handler(event(path={"id": CHAMADO_ID}), None)
            missing = obter.handler(
                event(path={"id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"}),
                None,
            )

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body(response)["id"], CHAMADO_ID)
        self.assertEqual(len(body(response)["historico"]), 1)
        self.assertEqual(missing["statusCode"], 404)
        self.assertTrue(all(call["ConsistentRead"] for call in table.query_calls))

    def test_listagem_usa_indice_filtros_e_token_de_paginacao(self):
        table = FakeTable(
            query_pages=[
                {
                    "Items": [{"id": "um"}],
                    "LastEvaluatedKey": {
                        "PK": f"CHAMADO#{CHAMADO_ID}",
                        "SK": "META",
                        "statusListaPk": "STATUS#ABERTO",
                        "criadoEmId": "2026-01-01#id",
                    },
                },
                {"Items": [{"id": "dois"}]},
            ]
        )
        request = event(
            query={"status": "aberto", "responsavel": "agente-1", "limit": "1"}
        )
        with patch.object(listar, "tabela", return_value=table):
            first = listar.handler(request, None)
            token = body(first)["nextToken"]
            second = listar.handler(
                event(
                    query={
                        "status": "ABERTO",
                        "responsavel": "agente-1",
                        "limit": "1",
                        "nextToken": token,
                    }
                ),
                None,
            )
            wrong_filter = listar.handler(
                event(query={"status": "FECHADO", "nextToken": token}),
                None,
            )

        self.assertEqual(body(first)["items"], [{"id": "um"}])
        self.assertTrue(token)
        self.assertEqual(table.query_calls[0]["IndexName"], "GSI-StatusLista")
        self.assertIn("FilterExpression", table.query_calls[0])
        self.assertEqual(body(second)["items"], [{"id": "dois"}])
        self.assertIsNone(body(second)["nextToken"])
        self.assertEqual(wrong_filter["statusCode"], 400)

    def test_listagem_por_responsavel_usa_gsi_dedicado(self):
        table = FakeTable(query_pages=[{"Items": [{"id": "atribuido"}]}])
        with patch.object(listar, "tabela", return_value=table):
            response = listar.handler(event(query={"responsavel": "agente-2"}), None)

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(table.query_calls[0]["IndexName"], "GSI-Responsavel")

    def test_listagem_geral_e_por_status_escolhe_indices_corretos(self):
        for filtros, indice in (
            ({}, "GSI-Criado"),
            ({"status": "FECHADO"}, "GSI-StatusLista"),
        ):
            table = FakeTable(query_pages=[{"Items": []}])
            with self.subTest(filtros=filtros), patch.object(
                listar, "tabela", return_value=table
            ):
                response = listar.handler(event(query=filtros), None)

            self.assertEqual(response["statusCode"], 200)
            self.assertEqual(table.query_calls[0]["IndexName"], indice)

    def test_listagem_com_filtro_continua_apos_pagina_sem_correspondencias(self):
        cursor = {
            "PK": f"CHAMADO#{CHAMADO_ID}",
            "SK": "META",
            "statusListaPk": "STATUS#ABERTO",
            "criadoEmId": "2026-01-01#id",
        }
        table = FakeTable(
            query_pages=[
                {"Items": [], "LastEvaluatedKey": cursor},
                {"Items": [{"id": "correspondente"}]},
            ]
        )
        with patch.object(listar, "tabela", return_value=table):
            response = listar.handler(
                event(
                    query={
                        "status": "ABERTO",
                        "responsavel": "agente-1",
                        "limit": "1",
                    }
                ),
                None,
            )

        self.assertEqual(body(response)["items"], [{"id": "correspondente"}])
        self.assertEqual(len(table.query_calls), 2)

    def test_patch_restringe_campos_e_atualiza_somente_os_solicitados(self):
        table = FakeTable(items=[self.chamado])
        with patch.object(atualizar, "tabela", return_value=table):
            response = atualizar.handler(
                event({"status": "resolvido"}, path={"id": CHAMADO_ID}),
                None,
            )
            invalid = atualizar.handler(
                event({"titulo": "não permitido"}, path={"id": CHAMADO_ID}),
                None,
            )

        self.assertEqual(response["statusCode"], 200)
        transaction = table.meta.client.transaction_calls[0]["TransactItems"]
        update = transaction[0]["Update"]
        self.assertEqual(
            deserialize_item(update["ExpressionAttributeValues"]),
            {
                ":status": "RESOLVIDO",
                ":statusPk": "RESOLVIDO#2026-01",
                ":statusListaPk": "STATUS#RESOLVIDO",
                ":atualizadoEm": body(response)["atualizadoEm"],
            },
        )
        evento = deserialize_item(transaction[1]["Put"]["Item"])
        self.assertEqual(evento["tipo"], "CHAMADO_ATUALIZADO")
        self.assertEqual(evento["alteracoes"]["status"]["para"], "RESOLVIDO")
        self.assertEqual(invalid["statusCode"], 400)

    def test_patch_para_critica_registra_outbox_na_mesma_transacao(self):
        table = FakeTable(items=[{**self.chamado, "prioridade": "MEDIA"}])
        with patch.object(atualizar, "tabela", return_value=table):
            response = atualizar.handler(
                event({"prioridade": "critica"}, path={"id": CHAMADO_ID}),
                None,
            )

        self.assertEqual(response["statusCode"], 200)
        transaction = table.meta.client.transaction_calls[0]["TransactItems"]
        self.assertEqual(len(transaction), 3)
        update = transaction[0]["Update"]
        self.assertIn("#prioridade = :prioridadeAnterior", update["ConditionExpression"])
        self.assertEqual(
            _DYNAMODB_DESERIALIZER.deserialize(
                update["ExpressionAttributeValues"][":prioridadeAnterior"]
            ),
            "MEDIA",
        )
        outbox = deserialize_item(transaction[2]["Put"]["Item"])
        self.assertEqual(outbox["eventType"], "chamado-critico")
        self.assertEqual(outbox["chamadoId"], CHAMADO_ID)
        self.assertEqual(outbox["prioridade"], "CRITICA")

    def test_patch_nao_republica_alerta_se_prioridade_ja_for_critica(self):
        table = FakeTable(items=[{**self.chamado, "prioridade": "CRITICA"}])
        with patch.object(atualizar, "tabela", return_value=table):
            response = atualizar.handler(
                event({"prioridade": "critica"}, path={"id": CHAMADO_ID}),
                None,
            )

        self.assertEqual(response["statusCode"], 200)
        transaction = table.meta.client.transaction_calls[0]["TransactItems"]
        self.assertEqual(len(transaction), 2)

    def test_comentario_usa_identidade_do_authorizer(self):
        table = FakeTable(items=[self.chamado])
        with patch.object(comentarios, "tabela", return_value=table):
            response = comentarios.handler(
                event(
                    {"texto": "Verificando"},
                    path={"id": CHAMADO_ID},
                    claims={"sub": "usuario-123"},
                ),
                None,
            )

        self.assertEqual(response["statusCode"], 201)
        self.assertEqual(body(response)["autorId"], "usuario-123")
        self.assertEqual(body(response)["tipo"], "COMENTARIO")

    def test_upload_valida_metadados_e_cria_url_com_limite_de_tamanho(self):
        table = FakeTable(items=[self.chamado])
        s3 = FakeS3()
        with (
            patch.object(anexos, "tabela", return_value=table),
            patch.object(anexos, "cliente_s3", return_value=s3),
            patch.dict(os.environ, {"BUCKET_ANEXOS": "bucket-teste"}),
        ):
            response = anexos.upload_handler(
                event(
                    {
                        "nomeArquivo": "../../log.txt",
                        "contentType": "text/plain",
                        "tamanho": 4,
                    },
                    path={"id": CHAMADO_ID},
                ),
                None,
            )
            invalid = anexos.upload_handler(
                event(
                    {
                        "nomeArquivo": "arquivo.exe",
                        "contentType": "application/x-msdownload",
                        "tamanho": 4,
                    },
                    path={"id": CHAMADO_ID},
                ),
                None,
            )

        self.assertEqual(response["statusCode"], 201)
        self.assertEqual(body(response)["expiresIn"], 300)
        self.assertEqual(s3.presigned_posts[0]["Conditions"][-1], ["content-length-range", 4, 4])
        stored_attachment = next(
            item for item in table.items.values() if item.get("anexoId")
        )
        self.assertIn("/log.txt", stored_attachment["chaveS3"])
        self.assertEqual(invalid["statusCode"], 400)

    def test_confirmacao_verifica_s3_e_e_idempotente(self):
        attachment = {
            "PK": f"CHAMADO#{CHAMADO_ID}",
            "SK": f"ANEXO#{ANEXO_ID}",
            "anexoId": ANEXO_ID,
            "chaveS3": f"anexos/{CHAMADO_ID}/{ANEXO_ID}/log.txt",
            "tamanho": 4,
            "contentType": "text/plain",
            "status": "PENDENTE",
            "ttl": 1,
        }
        table = FakeTable(items=[self.chamado, attachment])
        s3 = FakeS3()
        with (
            patch.object(anexos, "tabela", return_value=table),
            patch.object(anexos, "cliente_s3", return_value=s3),
            patch.dict(os.environ, {"BUCKET_ANEXOS": "bucket-teste"}),
        ):
            first = anexos.confirmar_handler(
                event({"anexoId": ANEXO_ID}, path={"id": CHAMADO_ID}),
                None,
            )
            second = anexos.confirmar_handler(
                event({"anexoId": ANEXO_ID}, path={"id": CHAMADO_ID}),
                None,
            )

        self.assertEqual(first["statusCode"], 200)
        self.assertEqual(second["statusCode"], 200)
        self.assertEqual(body(second)["status"], "CONFIRMADO")
        self.assertEqual(len(table.update_calls), 1)
        self.assertEqual(len(s3.tag_updates), 1)

    def test_confirmacao_rejeita_anexo_ausente_e_objeto_nao_enviado(self):
        table = FakeTable(items=[self.chamado])
        s3 = FakeS3()
        s3.head_error = ClientError(
            {"Error": {"Code": "NoSuchKey"}},
            "HeadObject",
        )
        with (
            patch.object(anexos, "tabela", return_value=table),
            patch.object(anexos, "cliente_s3", return_value=s3),
            patch.dict(os.environ, {"BUCKET_ANEXOS": "bucket-teste"}),
        ):
            missing = anexos.confirmar_handler(
                event({"anexoId": ANEXO_ID}, path={"id": CHAMADO_ID}),
                None,
            )
            table.put_item(
                Item={
                    "PK": f"CHAMADO#{CHAMADO_ID}",
                    "SK": f"ANEXO#{ANEXO_ID}",
                    "anexoId": ANEXO_ID,
                    "chaveS3": f"anexos/{CHAMADO_ID}/{ANEXO_ID}/log.txt",
                    "tamanho": 4,
                    "contentType": "text/plain",
                    "status": "PENDENTE",
                }
            )
            not_uploaded = anexos.confirmar_handler(
                event({"anexoId": ANEXO_ID}, path={"id": CHAMADO_ID}),
                None,
            )

        self.assertEqual(missing["statusCode"], 404)
        self.assertEqual(not_uploaded["statusCode"], 409)

    def test_download_so_gera_url_para_anexo_confirmado(self):
        attachment = {
            "PK": f"CHAMADO#{CHAMADO_ID}",
            "SK": f"ANEXO#{ANEXO_ID}",
            "anexoId": ANEXO_ID,
            "chaveS3": f"anexos/{CHAMADO_ID}/{ANEXO_ID}/log.txt",
            "status": "CONFIRMADO",
            "versionId": "version-1",
        }
        table = FakeTable(items=[self.chamado, attachment])
        s3 = FakeS3()
        with (
            patch.object(anexos, "tabela", return_value=table),
            patch.object(anexos, "cliente_s3", return_value=s3),
            patch.dict(os.environ, {"BUCKET_ANEXOS": "bucket-teste"}),
        ):
            response = anexos.download_handler(
                event(path={"id": CHAMADO_ID, "anexoId": ANEXO_ID}),
                None,
            )

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body(response)["expiresIn"], 300)
        self.assertEqual(s3.presigned_urls[0][0], "get_object")
        self.assertEqual(
            s3.presigned_urls[0][1]["Params"]["VersionId"],
            "version-1",
        )

    def test_publicador_envia_outbox_com_raw_evento_para_topico(self):
        sns = FakeSNS()
        item = {
            "PK": "OUTBOX#event-1",
            "SK": "EVENTO",
            "entityType": "OUTBOX",
            "eventId": "event-1",
            "eventType": "chamado-criado",
            "chamadoId": CHAMADO_ID,
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
        evento_critico = {
            **item,
            "PK": "OUTBOX#event-2",
            "eventId": "event-2",
            "eventType": "chamado-critico",
            "prioridade": "CRITICA",
        }
        with (
            patch.object(publicar, "cliente_sns", return_value=sns),
            patch.dict(
                os.environ,
                {
                    "TOPICO_CHAMADO_CRIADO": "arn:aws:sns:local:000000000000:criado",
                    "TOPICO_CHAMADO_CRITICO": "arn:aws:sns:local:000000000000:critico",
                },
            ),
        ):
            result = publicar.handler(
                {
                    "Records": [
                        {
                            "eventName": "INSERT",
                            "dynamodb": {"NewImage": serialize_item(item)},
                        },
                        {
                            "eventName": "INSERT",
                            "dynamodb": {"NewImage": serialize_item(evento_critico)},
                        },
                        {
                            "eventName": "INSERT",
                            "dynamodb": {
                                "NewImage": serialize_item(
                                    {"PK": f"CHAMADO#{CHAMADO_ID}", "SK": "META"}
                                )
                            },
                        },
                    ]
                },
                None,
            )

        self.assertEqual(result, {"publicados": True})
        self.assertEqual(len(sns.calls), 2)
        self.assertEqual(
            sns.calls[0]["TopicArn"],
            "arn:aws:sns:local:000000000000:criado",
        )
        self.assertEqual(
            json.loads(sns.calls[0]["Message"]),
            {
                "eventId": "event-1",
                "eventType": "chamado-criado",
                "chamadoId": CHAMADO_ID,
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        )
        self.assertEqual(
            sns.calls[1]["TopicArn"],
            "arn:aws:sns:local:000000000000:critico",
        )
        self.assertEqual(json.loads(sns.calls[1]["Message"])["prioridade"], "CRITICA")

    def test_publicador_propaga_falha_para_retry_do_stream(self):
        sns = FakeSNS(error=RuntimeError("SNS indisponível"))
        item = {
            "PK": "OUTBOX#event-1",
            "SK": "EVENTO",
            "entityType": "OUTBOX",
            "eventId": "event-1",
            "eventType": "chamado-criado",
            "chamadoId": CHAMADO_ID,
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
        with (
            patch.object(publicar, "cliente_sns", return_value=sns),
            patch.dict(
                os.environ,
                {
                    "TOPICO_CHAMADO_CRIADO": "arn:aws:sns:local:000000000000:criado",
                    "TOPICO_CHAMADO_CRITICO": "arn:aws:sns:local:000000000000:critico",
                },
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "SNS indisponível"):
                publicar.handler(
                    {
                        "Records": [
                            {
                                "eventName": "INSERT",
                                "dynamodb": {"NewImage": serialize_item(item)},
                            }
                        ]
                    },
                    None,
                )

    def test_atribuicao_fixa_atualiza_chamado_e_historico_idempotentemente(self):
        table = FakeTable(items=[self.chamado])
        with (
            patch.object(atribuir, "tabela", return_value=table),
            patch.dict(os.environ, {"ATENDENTE_DEMO_ID": "atendente-demo-01"}),
        ):
            first = atribuir.handler(sqs_event(CHAMADO_ID), None)
            duplicate = atribuir.handler(sqs_event(CHAMADO_ID), None)

        updated = table.items[(self.chamado["PK"], "META")]
        self.assertEqual(first, {"processados": 1})
        self.assertEqual(duplicate, {"processados": 0})
        self.assertEqual(updated["responsavelId"], "atendente-demo-01")
        self.assertEqual(updated["responsavelPk"], "RESPONSAVEL#atendente-demo-01")
        self.assertEqual(updated["status"], "EM_ATENDIMENTO")
        self.assertEqual(updated["statusPk"], "EM_ATENDIMENTO#2026-01")
        self.assertEqual(len(table.meta.client.transaction_calls), 1)
        transacao = table.meta.client.transaction_calls[0]["TransactItems"]
        atualizacao = transacao[0]["Update"]
        self.assertIn(
            "attribute_not_exists(#responsavelId)",
            atualizacao["ConditionExpression"],
        )
        historico = deserialize_item(transacao[1]["Put"]["Item"])
        self.assertEqual(historico["tipo"], "CHAMADO_ATRIBUIDO_AUTOMATICAMENTE")
        self.assertEqual(historico["responsavelId"], "atendente-demo-01")

    def test_atribuicao_nao_sobrescreve_responsavel_ou_status(self):
        for ticket in (
            {**self.chamado, "responsavelId": "atendente-manual"},
            {**self.chamado, "status": "RESOLVIDO"},
        ):
            table = FakeTable(items=[ticket])
            with (
                self.subTest(ticket=ticket),
                patch.object(atribuir, "tabela", return_value=table),
                patch.dict(os.environ, {"ATENDENTE_DEMO_ID": "atendente-demo-01"}),
            ):
                result = atribuir.handler(sqs_event(CHAMADO_ID), None)

            self.assertEqual(result, {"processados": 0})
            self.assertEqual(table.meta.client.transaction_calls, [])

    def test_atribuicao_reconhece_concorrencia_idempotente(self):
        table = FakeTable(items=[self.chamado])
        table.meta.client = ConcurrentAssignmentClient(table)
        with (
            patch.object(atribuir, "tabela", return_value=table),
            patch.dict(os.environ, {"ATENDENTE_DEMO_ID": "atendente-demo-01"}),
        ):
            result = atribuir.handler(sqs_event(CHAMADO_ID), None)

        self.assertEqual(result, {"processados": 0})
        self.assertEqual(
            table.items[(self.chamado["PK"], "META")]["responsavelId"],
            "atendente-demo-01",
        )

    def test_atribuicao_mensagem_invalida_e_chamado_ausente_falham(self):
        table = FakeTable()
        with (
            patch.object(atribuir, "tabela", return_value=table),
            patch.dict(os.environ, {"ATENDENTE_DEMO_ID": "atendente-demo-01"}),
        ):
            with self.assertRaisesRegex(ValueError, "JSON inválido"):
                atribuir.handler({"Records": [{"body": "{"}]}, None)
            with self.assertRaisesRegex(RuntimeError, "não encontrado"):
                atribuir.handler(sqs_event(CHAMADO_ID), None)


if __name__ == "__main__":
    unittest.main()