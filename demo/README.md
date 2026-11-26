# Demonstração Flask local

A interface usa a API já implantada no LocalStack. Ela lê `API_URL` de
`.localstack/test-state.env`, arquivo criado por `scripts/test_localstack.sh`,
ou aceita uma URL alternativa em `HELPDESK_API_URL`.

Antes de iniciar pela primeira vez, suba e implante a stack no LocalStack e
gere o arquivo de estado:

```bash
LOCALSTACK_REGION=us-east-1 bash scripts/test_localstack.sh
```

Na raiz do projeto:

```bash
.venv/bin/pip install -r demo/requirements.txt
.venv/bin/python -m demo.app
```

Abra <http://127.0.0.1:5000>. A aplicação é somente para demonstração local,
escuta em loopback e não adiciona autenticação. O navegador usa o Flask, que
encaminha as operações para a API e faz o upload/download dos anexos pelo S3.
O LocalStack precisa estar em execução. Para outra porta, defina `PORT`.

A interface permite criar, consultar, filtrar e paginar chamados; atualizar
status, prioridade e responsável; incluir comentários; e enviar/baixar anexos.
A atribuição continua sendo processada de forma assíncrona pela fila existente.
