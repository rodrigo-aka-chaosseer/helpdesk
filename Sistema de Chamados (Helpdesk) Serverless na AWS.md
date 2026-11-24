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
| Chamado | `CHAMADO#{id}` | `META` | titulo, descricao, categoria, prioridade, status, clienteId, responsavelId, criadoEm e chaves dos GSIs |
| Comentário | `CHAMADO#{id}` | `EVENTO#{timestamp}#{id}` | autorId, tipo, texto, criadoEm |
| Anexo | `CHAMADO#{id}` | `ANEXO#{anexoId}` | nomeArquivo, chaveS3, tamanho, contentType, status, criadoEm |
| Evento outbox | `OUTBOX#{eventId}` | `EVENTO` | eventType, chamadoId, timestamp, ttl |

### Índices secundários globais (GSI)

| GSI | Chave de partição | Chave de ordenação | Uso |
| --- | --- | --- | --- |
| `GSI-Criado` | `tipoPk` (`CHAMADO`) | `criadoEmId` (`criadoEm#id`) | Listagem geral, ordenada por data |
| `GSI-Status` | `statusPk` (`{status}#{ano-mes}`) | `criadoEm` | Índice mensal para consultas por status |
| `GSI-StatusLista` | `statusListaPk` (`STATUS#{status}`) | `criadoEmId` | Listagem da API por status, sem exigir intervalo de datas |
| `GSI-Responsavel` | `responsavelPk` (`RESPONSAVEL#{id}`) | `criadoEmId` | Listagem por atendente |

O endpoint de listagem consulta um GSI e usa `nextToken` opaco para paginação. Quando `status` e `responsavel` são enviados juntos, consulta `GSI-StatusLista` e aplica o segundo filtro na página do DynamoDB. Esse índice usa uma partição por status para permitir consultas sem intervalo de datas; em escala elevada, essa chave de baixa cardinalidade pode exigir particionamento/sharding adicional.

Chamados antigos precisam ter os atributos de GSI preenchidos para aparecer nos novos índices. A implantação dos GSIs também pode levar algum tempo até o estado `ACTIVE`.

### Modelo lógico de atendentes (ainda não implementado)

| PK | Atributos |
| --- | --- |
| `ATENDENTE#{id}` | nome, email, ativo, `cargaAtual` (contador atômico) |

Esse é apenas o modelo previsto: o template atual não provisiona uma tabela ou itens de atendentes, nem há um índice para localizar atendentes ativos. A fonte e o processo de cadastro ainda precisam ser definidos. A atribuição automática permanece desabilitada até essa decisão.

## Armazenamento de anexos (S3)

- **Bucket privado** com bloqueio de acesso público, criptografia em repouso (SSE-S3) e versionamento.
- **Chave do objeto:** `anexos/{chamadoId}/{anexoId}/{nomeArquivo}`.
- **Upload:** o cliente recebe uma URL pré-assinada de formulário POST (validade de 5 minutos), com limite de tamanho e tipo assinados, envia o arquivo diretamente ao S3 e depois confirma o envio à API.
- **Download:** também por URL pré-assinada, gerada sob demanda para a versão confirmada do objeto. Na implementação atual, sem authorizer, o handler valida apenas que o anexo foi confirmado; a autorização do usuário ainda precisa ser implementada.
- **Limites do protótipo:** máximo de 10 MiB e tipos `application/pdf`, `image/jpeg`, `image/png` e `text/plain`; confirmação também verifica tamanho, tipo e tag do objeto.
- **Ciclo de vida:** transição para uma classe mais barata (por exemplo, S3 Standard-IA) após 90 dias.
- **Limpeza de órfãos:** objetos com tag `status=pendente` expiram após 7 dias; a confirmação troca a tag para `status=confirmado`.

## Mensageria (SNS e SQS)

### Tópicos SNS

| Tópico | Publicado por | Assinantes |
| --- | --- | --- |
| `chamado-criado` | Evento outbox da criação, despachado por `PublicarEventosFn` | Fila SQS `atribuicao` |
| `chamado-critico` | Transição de prioridade para `CRITICA` via PATCH, despachada por `PublicarEventosFn` | E-mail ou SMS opcional da equipe de suporte |

### Filas SQS

| Fila | Consumidor | Configuração |
| --- | --- | --- |
| `atribuicao` | `AtribuirFn` (mapeamento desabilitado por padrão) | Visibility timeout de 60 s, timeout da Lambda de 10 s, retenção de 14 dias, `maxReceiveCount` de 3 |
| `atribuicao-dlq` | Análise manual | Retenção de 14 dias, alarme no CloudWatch quando houver mensagens |

### Publicação confiável e configuração

- A criação grava o chamado e um evento `chamado-criado` na tabela na mesma transação. Uma alteração de prioridade para `CRITICA` grava também o evento `chamado-critico` na mesma transação do PATCH. A publicação SNS ocorre de forma assíncrona pelo stream DynamoDB; assim, uma falha no SNS não transforma uma gravação já concluída em erro HTTP nem perde o evento imediatamente.
- O contrato publicado é JSON com `eventId`, `eventType`, `chamadoId` e `timestamp` (e `prioridade` para chamados críticos). A assinatura SNS→SQS usa entrega bruta (`RawMessageDelivery`).
- O publisher tenta novamente até três vezes; falhas persistentes vão para `publicacao-eventos-dlq`. O TTL de 30 dias limpa os itens outbox. Streams e SNS podem entregar mais de uma vez; consumidores devem ser idempotentes.
- `HabilitarAtribuicao` tem padrão `false`; habilite-o explicitamente para executar a demonstração. `AtendenteDemoId` define o ID fixo atribuído pelo consumidor (padrão `atendente-demo-01`); esse valor não é validado contra um cadastro real.
- A assinatura de alerta crítico é opcional: configure `ProtocoloAlertaCritico` (`email` ou `sms`) e `DestinoAlertaCritico`. Assinaturas de e-mail exigem confirmação do destinatário.
- Os alarmes das DLQs são criados sem destino de notificação. `TopicoAlarmeDlqArn` pode apontar para um tópico SNS existente para receber ações dos alarmes.
- A prioridade inicial continua sendo `MEDIA`; não existe classificador automático. O alerta crítico é publicado somente quando um PATCH efetua a transição de outra prioridade para `CRITICA`.
- `sam local start-api` não provisiona por si só os recursos AWS do template no LocalStack. Antes de testar a mensageria, implante/provisione também os tópicos, filas, políticas, stream DynamoDB e alarmes. LocalStack pode variar no suporte a DynamoDB Streams, event source mappings e CloudWatch Alarms; valide esses recursos no ambiente de integração. Os testes unitários mockam SNS e DynamoDB.

## Fluxos principais

### Abertura de chamado

1. O cliente envia `POST /chamados` ao API Gateway.
2. A Lambda de criação valida os dados e grava o chamado com status `ABERTO`, prioridade inicial `MEDIA` e evento outbox na mesma transação.
3. `PublicarEventosFn` lê o outbox pelo DynamoDB Streams e publica no tópico `chamado-criado`.
4. O SNS entrega o evento bruto à fila `atribuicao`. Quando `HabilitarAtribuicao=true`, o consumidor atribui o ID configurado por `AtendenteDemoId`.
5. A API responde `201` com o identificador do chamado após a transação do DynamoDB.
6. Um PATCH que muda a prioridade para `CRITICA` grava atomicamente o evento de alerta; o publisher o envia ao tópico `chamado-critico`, se houver assinatura configurada.

### Listagem, consulta e atualização

- `GET /chamados` aceita `status`, `responsavel`, `limit` (1 a 100) e `nextToken`. A resposta contém `items` e `nextToken`.
- `GET /chamados/{id}` mantém os atributos do chamado na resposta e acrescenta `historico`, ordenado por data.
- `PATCH /chamados/{id}` aceita `status`, `prioridade` e `responsavelId`. A alteração do chamado e o evento de histórico são gravados atomicamente. A transição para `CRITICA` também grava um evento outbox na mesma transação. Enviar `responsavelId: null` remove a atribuição.
- Status aceitos: `ABERTO`, `EM_ATENDIMENTO`, `RESOLVIDO` e `FECHADO`. Prioridades: `BAIXA`, `MEDIA`, `ALTA` e `CRITICA`.

### Comentários e confirmação de anexos

- `POST /chamados/{id}/comentarios` recebe `texto`; o autor é derivado das claims do authorizer quando disponíveis, e não de um `autorId` enviado no corpo.
- `POST /chamados/{id}/anexos` recebe `nomeArquivo`, `contentType` e `tamanho`; a resposta contém `anexoId`, `uploadUrl`, `fields` e `expiresIn`.
- `POST /chamados/{id}/anexos/confirmacao` recebe `anexoId`. Repetir a confirmação de um anexo já confirmado retorna sucesso sem criar outro registro.
- `GET /chamados/{id}/anexos/{anexoId}` retorna `url` e `expiresIn` somente para anexos confirmados.

### Envio de anexo

1. O cliente chama `POST /chamados/{id}/anexos` informando nome, tipo e tamanho.
2. A Lambda valida as regras e devolve uma URL pré-assinada de upload.
3. O cliente envia o arquivo diretamente ao S3.
4. O cliente chama `POST /chamados/{id}/anexos/confirmacao`, e a Lambda registra o anexo no chamado.

### Atribuição automática

Para a demonstração, `AtribuirFn` usa um único ID fixo configurável por `AtendenteDemoId`; não consulta uma tabela de atendentes nem distribui carga. Ao receber `chamado-criado`, lê o chamado consistentemente e, se ele ainda estiver `ABERTO` e sem responsável, grava `responsavelId`, `responsavelPk`, status `EM_ATENDIMENTO` e um evento de histórico na mesma transação. A condição de escrita impede sobrescrever atribuições manuais e torna reentregas idempotentes. Se o chamado já tiver responsável ou não estiver aberto, a mensagem é reconhecida sem alteração. O mapeamento SQS→Lambda continua desabilitado por padrão; defina `HabilitarAtribuicao=true` explicitamente para a demonstração. Esse atendente é apenas um identificador de demonstração, não uma identidade autenticada nem um cadastro validado.

## Segurança

- **Autenticação:** Amazon Cognito (User Pool) com dois grupos, `clientes` e `atendentes`, validado por um authorizer no API Gateway. Para um protótipo, pode-se usar uma chave de API ou um authorizer Lambda simples.
- **Autorização:** a Lambda confere se o cliente só acessa os próprios chamados e se as ações de atendimento exigem o grupo `atendentes`.
- **IAM:** uma role por Lambda, restrita às tabelas, ao prefixo do bucket e aos tópicos ou filas que ela usa.
- **Dados:** criptografia em repouso (DynamoDB e S3) e em trânsito (HTTPS).

**Limitações da implementação atual:** não há authorizer Cognito nem autorização por papel/cliente. O `responsavelId` do PATCH e o ID fixo de demonstração não são validados contra uma lista de atendentes. A atribuição implementada é apenas para demonstração; não há cadastro, distribuição de carga ou classificação automática. Os alertas críticos dependem de uma transição para `CRITICA` via PATCH. Quando a API é chamada sem claims, comentários usam `autorId: "anonimo"` e chamados não recebem `clienteId`; portanto, a API ainda não deve ser exposta a usuários finais até que autenticação e autorização sejam implementadas.
