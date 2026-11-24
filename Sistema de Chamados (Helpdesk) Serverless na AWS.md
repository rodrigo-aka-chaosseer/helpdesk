# Sistema de Chamados (Helpdesk) Serverless na AWS

Definição do sistema e arquitetura

## Visão geral

O sistema permite que clientes abram chamados de suporte com anexos (prints, logs, documentos) e que a equipe de atendimento acompanhe, atribua e resolva esses chamados. A solução é totalmente **serverless**, construída sobre cinco serviços da AWS: **API Gateway**, **Lambda**, **DynamoDB**, **S3** e **SQS/SNS**.

### Objetivos

- Permitir a abertura, consulta, atualização e encerramento de chamados por uma API REST.
- Armazenar anexos de forma segura e barata, com upload direto ao S3.
- Classificar a prioridade dos chamados e notificar a equipe quando um chamado for crítico.
- Distribuir os chamados entre os atendentes de forma automática e justa.
- Operar sem servidores para gerenciar, com custo proporcional ao uso.

### Fora do escopo

- Interface web completa (o foco é o back-end e a API).
- Chat em tempo real entre cliente e atendente.
- Relatórios analíticos avançados.

## Atores

| Ator | Descrição | Principais ações |
| --- | --- | --- |
| Cliente | Usuário que precisa de suporte | Abrir chamado, anexar arquivos, consultar status, comentar |
| Atendente | Membro da equipe de suporte | Ver fila, assumir chamado, alterar status, responder, encerrar |
| Administrador | Gestor do suporte | Cadastrar atendentes, ajustar regras de prioridade |

## Arquitetura

### Componentes e responsabilidades

| Serviço | Papel no sistema |
| --- | --- |
| API Gateway | Ponto de entrada REST, validação de requisições, autenticação via authorizer |
| Lambda | Regras de negócio da API, geração de URL pré-assinada, classificação de prioridade, atribuição automática |
| DynamoDB | Persistência de chamados, comentários, atendentes e contadores de carga |
| S3 | Armazenamento dos anexos, com ciclo de vida e criptografia |
| SNS | Publicação de eventos de domínio (`chamado-criado`, `chamado-critico`) e envio de alertas |
| SQS | Fila de atribuição automática, com DLQ, para processamento assíncrono e controle de concorrência |

### Visão lógica

```
Cliente / Atendente
        |
        v
  API Gateway  --(authorizer)-->  Cognito ou chave de API
        |
        v
  Lambdas da API (criar, consultar, atualizar, anexar)
     |            |                 |
     v            v                 v
 DynamoDB   S3 (URL pre-assinada)   SNS: chamado-criado
                                        |           |
                                        v           v
                              SQS: atribuicao    SNS: chamado-critico
                                  |    (DLQ)           |
                                  v                    v
                        Lambda de atribuicao     E-mail / SMS
                                  |              da equipe
                                  v
                              DynamoDB
```

### Endpoints da API

| Método | Rota | Descrição |
| --- | --- | --- |
| POST | `/chamados` | Cria um chamado |
| GET | `/chamados` | Lista chamados (filtros `status`, `responsavel`) |
| GET | `/chamados/{id}` | Consulta um chamado com histórico |
| PATCH | `/chamados/{id}` | Atualiza status, prioridade ou responsável |
| POST | `/chamados/{id}/comentarios` | Adiciona um comentário |
| POST | `/chamados/{id}/anexos` | Solicita URL pré-assinada para upload |
| POST | `/chamados/{id}/anexos/confirmacao` | Confirma o anexo enviado e o registra no chamado |
| GET | `/chamados/{id}/anexos/{anexoId}` | Retorna URL pré-assinada para download |

## Modelo de dados (DynamoDB)

O esquema parte dos **padrões de acesso**, não das entidades.

### Padrões de acesso

1. Buscar um chamado pelo identificador.
2. Listar os chamados de um cliente.
3. Listar os chamados por status (fila de atendimento).
4. Listar os chamados de um atendente.
5. Listar o histórico (comentários e eventos) de um chamado.
6. Consultar e incrementar a carga de cada atendente.

### Tabela `Chamados` (single table)

| Item | PK | SK | Atributos principais |
| --- | --- | --- | --- |
| Chamado | `CHAMADO#{id}` | `META` | titulo, descricao, categoria, prioridade, status, clienteId, responsavelId, criadoEm |
| Comentário | `CHAMADO#{id}` | `EVENTO#{timestamp}` | autorId, tipo, texto |
| Anexo | `CHAMADO#{id}` | `ANEXO#{anexoId}` | nomeArquivo, chaveS3, tamanho, contentType |

### Índices secundários globais (GSI)

| GSI | Chave de partição | Chave de ordenação | Uso |
| --- | --- | --- | --- |
| `GSI-Status` | `status#ano-mes` | `criadoEm` | Fila por status. A chave composta evita partição quente |
| `GSI-Responsavel` | `responsavelId` | `criadoEm` | Chamados de cada atendente |
| `GSI-Cliente` | `clienteId` | `criadoEm` | Chamados de cada cliente |

### Tabela `Atendentes`

| PK | Atributos |
| --- | --- |
| `ATENDENTE#{id}` | nome, email, ativo, `cargaAtual` (contador atômico) |

A atribuição incrementa `cargaAtual` com `UpdateItem` e `ADD`, de forma atômica. Ao encerrar um chamado, a carga é decrementada.

## Armazenamento de anexos (S3)

- **Bucket privado** com bloqueio de acesso público e criptografia em repouso (SSE-S3).
- **Chave do objeto:** `anexos/{chamadoId}/{anexoId}/{nomeArquivo}`.
- **Upload:** o cliente recebe uma URL pré-assinada (validade de 5 minutos), envia o arquivo direto ao S3 e depois confirma o envio à API.
- **Download:** também por URL pré-assinada, gerada sob demanda após checar a permissão do usuário.
- **Limites:** tamanho máximo e tipos de arquivo permitidos validados na geração da URL.
- **Ciclo de vida:** transição para uma classe mais barata (por exemplo, S3 Standard-IA) após 90 dias.
- **Limpeza de órfãos:** objetos enviados sem confirmação são removidos por uma regra de ciclo de vida ou por uma tag de "pendente" removida na confirmação.

## Mensageria (SNS e SQS)

### Tópicos SNS

| Tópico | Publicado por | Assinantes |
| --- | --- | --- |
| `chamado-criado` | Lambda de criação | Fila SQS `atribuicao` |
| `chamado-critico` | Lambda de classificação | E-mail ou SMS da equipe de suporte |

### Filas SQS

| Fila | Consumidor | Configuração |
| --- | --- | --- |
| `atribuicao` | Lambda de atribuição | Visibility timeout maior que o timeout da Lambda, `maxReceiveCount` de 3 |
| `atribuicao-dlq` | Análise manual | Retenção de 14 dias, alarme no CloudWatch quando houver mensagens |

## Fluxos principais

### Abertura de chamado

1. O cliente envia `POST /chamados` ao API Gateway.
2. A Lambda de criação valida os dados, classifica a prioridade e grava o chamado com status `ABERTO`.
3. A Lambda publica o evento no tópico `chamado-criado`.
4. Se a prioridade for `CRITICA`, a Lambda também publica em `chamado-critico`, e a equipe é alertada.
5. A API responde `201` com o identificador do chamado.

### Envio de anexo

1. O cliente chama `POST /chamados/{id}/anexos` informando nome, tipo e tamanho.
2. A Lambda valida as regras e devolve uma URL pré-assinada de upload.
3. O cliente envia o arquivo diretamente ao S3.
4. O cliente chama `POST /chamados/{id}/anexos/confirmacao`, e a Lambda registra o anexo no chamado.

### Atribuição automática

1. A fila `atribuicao` recebe o evento `chamado-criado` do SNS.
2. A Lambda de atribuição lê a mensagem e consulta os atendentes ativos.
3. Escolhe o atendente com menor `cargaAtual`.
4. Atualiza o chamado com uma escrita condicional (`responsavelId` ainda vazio), o que garante **idempotência** caso a mensagem seja reentregue.
5. Incrementa `cargaAtual` do atendente e muda o status para `EM_ATENDIMENTO`.
6. Em caso de falha repetida, a mensagem segue para a DLQ.

## Segurança

- **Autenticação:** Amazon Cognito (User Pool) com dois grupos, `clientes` e `atendentes`, validado por um authorizer no API Gateway. Para um protótipo, pode-se usar uma chave de API ou um authorizer Lambda simples.
- **Autorização:** a Lambda confere se o cliente só acessa os próprios chamados e se as ações de atendimento exigem o grupo `atendentes`.
- **IAM:** uma role por Lambda, restrita às tabelas, ao prefixo do bucket e aos tópicos ou filas que ela usa.
- **Dados:** criptografia em repouso (DynamoDB e S3) e em trânsito (HTTPS).
