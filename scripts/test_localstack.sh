#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENDPOINT="${LOCALSTACK_ENDPOINT:-http://localhost:4566}"
REGION="${LOCALSTACK_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
STACK_NAME="${STACK_NAME:-helpdesk-local}"
STATE_FILE="$ROOT_DIR/.localstack/test-state.env"
WITH_DLQ_TEST=false

for argument in "$@"; do
  case "$argument" in
    --with-dlq)
      WITH_DLQ_TEST=true
      ;;
    -h|--help)
      printf 'Uso: bash scripts/test_localstack.sh [--with-dlq]\n'
      printf 'STACK_NAME, LOCALSTACK_ENDPOINT e LOCALSTACK_REGION podem ser definidos no ambiente.\n'
      exit 0
      ;;
    *)
      printf 'Argumento desconhecido: %s\n' "$argument" >&2
      exit 2
      ;;
  esac
done

if [[ ! "$REGION" =~ ^[a-z]{2}(-gov)?-[a-z]+-[0-9]+$ ]]; then
  printf 'Região inválida: <%s>. Use, por exemplo, LOCALSTACK_REGION=us-east-1.\n' \
    "$REGION" >&2
  exit 2
fi

for dependency in aws curl jq python3; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    printf 'Dependência ausente: %s\n' "$dependency" >&2
    exit 2
  fi
done

export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION="$REGION"
export AWS_EC2_METADATA_DISABLED=true

TMP_DIR="$(mktemp -d)"
RESPONSE_FILE="$TMP_DIR/response.json"
ANEXO_FILE="$TMP_DIR/anexo.txt"
DOWNLOAD_FILE="$TMP_DIR/anexo-baixado.txt"
CRITICAL_QUEUE_URL=""
CRITICAL_SUBSCRIPTION_ARN=""
API_ID=""
API_URL=""
DEMO_AGENT_ID=""
CHAMADO_ID=""
CHAMADO_PAGINACAO_ID=""
ANEXO_ID=""

aws_local() {
  aws --endpoint-url="$ENDPOINT" --region "$REGION" "$@"
}

fail() {
  printf 'FALHOU: %s\n' "$*" >&2
  exit 1
}

pass() {
  printf 'OK: %s\n' "$*"
}

cleanup() {
  if [[ -n "$CRITICAL_SUBSCRIPTION_ARN" ]]; then
    aws_local sns unsubscribe \
      --subscription-arn "$CRITICAL_SUBSCRIPTION_ARN" >/dev/null 2>&1 || true
  fi
  if [[ -n "$CRITICAL_QUEUE_URL" ]]; then
    aws_local sqs delete-queue \
      --queue-url "$CRITICAL_QUEUE_URL" >/dev/null 2>&1 || true
  fi
  rm -r -- "$TMP_DIR"
}
trap cleanup EXIT

save_state() {
  mkdir -p "$(dirname "$STATE_FILE")"
  umask 077
  {
    printf 'STACK_NAME=%q\n' "$STACK_NAME"
    printf 'API_ID=%q\n' "$API_ID"
    printf 'API_URL=%q\n' "$API_URL"
    printf 'DEMO_AGENT_ID=%q\n' "$DEMO_AGENT_ID"
    printf 'CHAMADO_ID=%q\n' "$CHAMADO_ID"
    printf 'CHAMADO_PAGINACAO_ID=%q\n' "$CHAMADO_PAGINACAO_ID"
    printf 'ANEXO_ID=%q\n' "$ANEXO_ID"
  } >"$STATE_FILE"
  chmod 600 "$STATE_FILE"
}

api_request() {
  local method="$1"
  local path="$2"
  local payload="${3-}"
  local -a arguments=(-sS --max-time 30 -o "$RESPONSE_FILE" -w '%{http_code}')

  if [[ -n "$payload" ]]; then
    arguments+=(-X "$method" -H 'Content-Type: application/json' -d "$payload")
  else
    arguments+=(-X "$method")
  fi

  HTTP_STATUS="$(curl "${arguments[@]}" "$API_URL$path")" ||
    fail "não foi possível acessar $API_URL$path"
  HTTP_BODY="$(<"$RESPONSE_FILE")"
}

expect_status() {
  local expected="$1"
  local description="$2"
  if [[ "$HTTP_STATUS" != "$expected" ]]; then
    fail "$description: esperado HTTP $expected, recebido HTTP $HTTP_STATUS"
  fi
  pass "$description (HTTP $HTTP_STATUS)"
}

create_ticket() {
  local title="$1"
  local response_id
  api_request POST /chamados \
    "$(jq -cn --arg titulo "$title" \
      '{titulo:$titulo,descricao:"Chamado criado pelo teste automatizado LocalStack",categoria:"Teste"}')"
  if [[ "$HTTP_STATUS" == "502" ]]; then
    fail "POST /chamados retornou 502; reimplante o código atual no LocalStack e confira os logs da Lambda"
  fi
  expect_status 201 "POST /chamados" >&2
  response_id="$(jq -er '.id | strings | select(test("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"))' <<<"$HTTP_BODY")" ||
    fail "POST /chamados não retornou um UUID válido"
  printf '%s' "$response_id"
}

wait_for_assignment() {
  local ticket_id="$1"
  local attempt
  for attempt in {1..30}; do
    api_request GET "/chamados/$ticket_id"
    if [[ "$HTTP_STATUS" == "200" ]] &&
      jq -e --arg owner "$DEMO_AGENT_ID" \
        '.responsavelId == $owner and .status == "EM_ATENDIMENTO" and any(.historico[]?; .tipo == "CHAMADO_ATRIBUIDO_AUTOMATICAMENTE")' \
        >/dev/null <<<"$HTTP_BODY"; then
      pass "atribuição SNS → SQS → Lambda → DynamoDB ($ticket_id)"
      return
    fi
    sleep 2
  done
  fail "atribuição não apareceu em 60 s para o chamado $ticket_id; confira HabilitarAtribuicao e os logs da Lambda"
}

printf 'Verificando LocalStack em %s...\n' "$ENDPOINT"
curl -fsS --max-time 5 "$ENDPOINT/_localstack/health" >/dev/null ||
  fail "LocalStack não respondeu em $ENDPOINT"

STACK_STATUS="$(aws_local cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].StackStatus' \
  --output text)" || fail "stack $STACK_NAME não encontrada no LocalStack"
case "$STACK_STATUS" in
  CREATE_COMPLETE|UPDATE_COMPLETE) ;;
  *) fail "stack $STACK_NAME está em estado $STACK_STATUS" ;;
esac
pass "stack $STACK_NAME ($STACK_STATUS)"

STACK_RESOURCES="$(aws_local cloudformation list-stack-resources \
  --stack-name "$STACK_NAME" \
  --output json)"
API_ID="$(jq -r '.StackResourceSummaries[] | select(.LogicalResourceId == "ServerlessRestApi") | .PhysicalResourceId' <<<"$STACK_RESOURCES")"
[[ -n "$API_ID" && "$API_ID" != "null" ]] ||
  fail "não encontrei o recurso API Gateway ServerlessRestApi na stack"

DEMO_AGENT_ID="$(aws_local cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Parameters[?ParameterKey=='AtendenteDemoId'].ParameterValue | [0]" \
  --output text)"
ASSIGNMENT_ENABLED="$(aws_local cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Parameters[?ParameterKey=='HabilitarAtribuicao'].ParameterValue | [0]" \
  --output text)"
[[ -n "$DEMO_AGENT_ID" && "$DEMO_AGENT_ID" != "None" ]] ||
  fail "a stack não informa o parâmetro AtendenteDemoId"
[[ "$ASSIGNMENT_ENABLED" == "true" ]] ||
  fail "implante a stack com HabilitarAtribuicao=true para testar a atribuição"

API_URL="$ENDPOINT/restapis/$API_ID/Prod/_user_request_"
save_state
printf 'API_ID=%s\nAPI_URL=%s\n' "$API_ID" "$API_URL"

API_RESOURCE_METHODS="$(aws_local apigateway get-resources \
  --rest-api-id "$API_ID" \
  --query "items[?path=='/chamados'].resourceMethods" \
  --output json)"
jq -e 'length > 0' >/dev/null <<<"$API_RESOURCE_METHODS" ||
  fail "a API não possui a rota /chamados"
pass "rotas REST encontradas no API Gateway"

CHAMADO_ID="$(create_ticket "Teste LocalStack $(date +%s)")"
save_state
wait_for_assignment "$CHAMADO_ID"

CHAMADO_PAGINACAO_ID="$(create_ticket "Teste paginação $(date +%s)")"
save_state
wait_for_assignment "$CHAMADO_PAGINACAO_ID"

api_request GET "/chamados/$CHAMADO_ID"
expect_status 200 "GET /chamados/{id}"
jq -e --arg id "$CHAMADO_ID" \
  '.id == $id and (.historico | type == "array")' \
  >/dev/null <<<"$HTTP_BODY" ||
  fail "consulta individual não retornou o chamado e seu histórico"

api_request GET "/chamados?status=EM_ATENDIMENTO&responsavel=$DEMO_AGENT_ID"
expect_status 200 "GET /chamados com filtros combinados"
jq -e --arg id "$CHAMADO_ID" \
  '.items | any(.id == $id)' >/dev/null <<<"$HTTP_BODY" ||
  fail "listagem filtrada não incluiu o chamado esperado"

api_request GET "/chamados?status=EM_ATENDIMENTO"
expect_status 200 "GET /chamados filtrado por status"
api_request GET "/chamados?responsavel=$DEMO_AGENT_ID"
expect_status 200 "GET /chamados filtrado por responsável"

api_request GET /chamados?limit=1
expect_status 200 "GET /chamados com limite de paginação"
NEXT_TOKEN="$(jq -r '.nextToken // empty' <<<"$HTTP_BODY")"
[[ -n "$NEXT_TOKEN" ]] || fail "paginação não retornou nextToken com dois chamados"
ENCODED_TOKEN="$(python3 -c 'from urllib.parse import quote; import sys; print(quote(sys.argv[1], safe=""))' "$NEXT_TOKEN")"
api_request GET "/chamados?limit=1&nextToken=$ENCODED_TOKEN"
expect_status 200 "GET /chamados usando nextToken"
jq -e 'has("items") and has("nextToken")' >/dev/null <<<"$HTTP_BODY" ||
  fail "resposta da segunda página não tem o formato esperado"
pass "paginação retornou duas páginas"

api_request PATCH "/chamados/$CHAMADO_ID" \
  '{"status":"RESOLVIDO","prioridade":"ALTA","responsavelId":"atendente-script"}'
expect_status 200 "PATCH /chamados/{id}"
jq -e '.status == "RESOLVIDO" and .prioridade == "ALTA" and .responsavelId == "atendente-script"' \
  >/dev/null <<<"$HTTP_BODY" ||
  fail "PATCH não atualizou os campos solicitados"

api_request GET "/chamados?status=RESOLVIDO&responsavel=atendente-script"
expect_status 200 "listagem após PATCH"
jq -e --arg id "$CHAMADO_ID" \
  '.items | any(.id == $id)' >/dev/null <<<"$HTTP_BODY" ||
  fail "listagem filtrada não refletiu os campos atualizados"

api_request POST "/chamados/$CHAMADO_ID/comentarios" \
  '{"texto":"Comentário criado pelo teste automatizado"}'
expect_status 201 "POST /chamados/{id}/comentarios"

api_request GET "/chamados/$CHAMADO_ID"
jq -e 'any(.historico[]?; .tipo == "COMENTARIO" and .texto == "Comentário criado pelo teste automatizado")' \
  >/dev/null <<<"$HTTP_BODY" ||
  fail "comentário não apareceu no histórico"
pass "comentário aparece no histórico"

printf 'Arquivo de teste LocalStack\n' >"$ANEXO_FILE"
ANEXO_TAMANHO="$(wc -c <"$ANEXO_FILE" | tr -d ' ')"
api_request POST "/chamados/$CHAMADO_ID/anexos" \
  "$(jq -cn --argjson tamanho "$ANEXO_TAMANHO" \
    '{nomeArquivo:"teste-localstack.txt",contentType:"text/plain",tamanho:$tamanho}')"
expect_status 201 "POST /chamados/{id}/anexos"
ANEXO_ID="$(jq -er '.anexoId' <<<"$HTTP_BODY")"
UPLOAD_URL="$(jq -er '.uploadUrl' <<<"$HTTP_BODY")"
[[ "$(jq -r '.expiresIn' <<<"$HTTP_BODY")" == "300" ]] ||
  fail "URL de upload não tem validade esperada de 300 segundos"
save_state

UPLOAD_HOST="$(python3 -c 'from urllib.parse import urlparse; import sys; print(urlparse(sys.argv[1]).hostname)' "$UPLOAD_URL")"
UPLOAD_PORT="$(python3 -c 'from urllib.parse import urlparse; import sys; u=urlparse(sys.argv[1]); print(u.port or (443 if u.scheme == "https" else 80))' "$UPLOAD_URL")"
FORM_ARGUMENTS=()
while IFS=$'\t' read -r key value; do
  FORM_ARGUMENTS+=(--form-string "$key=$value")
done < <(jq -r '.fields | to_entries[] | [.key, (.value|tostring)] | @tsv' <<<"$HTTP_BODY")

UPLOAD_STATUS="$(curl -sS --max-time 30 -o "$RESPONSE_FILE" -w '%{http_code}' \
  --resolve "$UPLOAD_HOST:$UPLOAD_PORT:127.0.0.1" \
  "${FORM_ARGUMENTS[@]}" \
  -F "file=@$ANEXO_FILE;type=text/plain" \
  "$UPLOAD_URL")" || fail "upload pré-assinado ao S3 falhou"
case "$UPLOAD_STATUS" in
  200|201|204) pass "upload do arquivo ao S3 (HTTP $UPLOAD_STATUS)" ;;
  *)
    S3_ERROR_CODE="$(python3 - "$RESPONSE_FILE" <<'PY'
import sys
import xml.etree.ElementTree as ET

try:
    root = ET.parse(sys.argv[1]).getroot()
    print(root.findtext("Code", default="Unknown"))
except ET.ParseError:
    print("Unknown")
PY
)"
    fail "upload ao S3 retornou HTTP $UPLOAD_STATUS (código: $S3_ERROR_CODE)"
    ;;
esac

api_request POST "/chamados/$CHAMADO_ID/anexos/confirmacao" \
  "$(jq -cn --arg id "$ANEXO_ID" '{anexoId:$id}')"
expect_status 200 "POST /chamados/{id}/anexos/confirmacao"
api_request POST "/chamados/$CHAMADO_ID/anexos/confirmacao" \
  "$(jq -cn --arg id "$ANEXO_ID" '{anexoId:$id}')"
expect_status 200 "confirmação repetida do anexo (idempotência)"

api_request GET "/chamados/$CHAMADO_ID/anexos/$ANEXO_ID"
expect_status 200 "GET /chamados/{id}/anexos/{anexoId}"
DOWNLOAD_URL="$(jq -er '.url' <<<"$HTTP_BODY")"
DOWNLOAD_HOST="$(python3 -c 'from urllib.parse import urlparse; import sys; print(urlparse(sys.argv[1]).hostname)' "$DOWNLOAD_URL")"
DOWNLOAD_PORT="$(python3 -c 'from urllib.parse import urlparse; import sys; u=urlparse(sys.argv[1]); print(u.port or (443 if u.scheme == "https" else 80))' "$DOWNLOAD_URL")"
curl -fsS --max-time 30 \
  --resolve "$DOWNLOAD_HOST:$DOWNLOAD_PORT:127.0.0.1" \
  "$DOWNLOAD_URL" -o "$DOWNLOAD_FILE" ||
  fail "download pré-assinado do S3 falhou"
cmp -s "$ANEXO_FILE" "$DOWNLOAD_FILE" ||
  fail "arquivo baixado não corresponde ao arquivo enviado"
pass "download do anexo e comparação do conteúdo"

CRITICAL_TOPIC_ARN="$(aws_local sns list-topics \
  --query "Topics[?contains(TopicArn, 'chamado-critico')].TopicArn | [0]" \
  --output text)"
[[ -n "$CRITICAL_TOPIC_ARN" && "$CRITICAL_TOPIC_ARN" != "None" ]] ||
  fail "tópico SNS chamado-critico não encontrado"

CRITICAL_QUEUE_NAME="helpdesk-teste-critico-$$"
CRITICAL_QUEUE_URL="$(aws_local sqs create-queue \
  --queue-name "$CRITICAL_QUEUE_NAME" \
  --query QueueUrl --output text)"
CRITICAL_QUEUE_ARN="$(aws_local sqs get-queue-attributes \
  --queue-url "$CRITICAL_QUEUE_URL" \
  --attribute-names QueueArn \
  --query Attributes.QueueArn --output text)"
QUEUE_POLICY="$(jq -cn --arg queue "$CRITICAL_QUEUE_ARN" --arg topic "$CRITICAL_TOPIC_ARN" \
  '{Version:"2012-10-17",Statement:[{Effect:"Allow",Principal:{Service:"sns.amazonaws.com"},Action:"sqs:SendMessage",Resource:$queue,Condition:{ArnEquals:{"aws:SourceArn":$topic}}}]}')"
aws_local sqs set-queue-attributes \
  --queue-url "$CRITICAL_QUEUE_URL" \
  --attributes "$(jq -cn --arg policy "$QUEUE_POLICY" '{Policy:$policy}')" >/dev/null
CRITICAL_SUBSCRIPTION_ARN="$(aws_local sns subscribe \
  --topic-arn "$CRITICAL_TOPIC_ARN" \
  --protocol sqs \
  --notification-endpoint "$CRITICAL_QUEUE_ARN" \
  --attributes RawMessageDelivery=true \
  --query SubscriptionArn --output text)"

api_request PATCH "/chamados/$CHAMADO_ID" '{"prioridade":"CRITICA"}'
expect_status 200 "PATCH para prioridade CRITICA"
CRITICAL_MESSAGE=""
for attempt in {1..8}; do
  CRITICAL_MESSAGE="$(aws_local sqs receive-message \
    --queue-url "$CRITICAL_QUEUE_URL" \
    --wait-time-seconds 5 \
    --max-number-of-messages 1 \
    --query 'Messages[0].Body' \
    --output text)"
  if [[ -n "$CRITICAL_MESSAGE" && "$CRITICAL_MESSAGE" != "None" ]]; then
    break
  fi
done
jq -e --arg id "$CHAMADO_ID" \
  '.eventType == "chamado-critico" and .chamadoId == $id and .prioridade == "CRITICA"' \
  >/dev/null <<<"$CRITICAL_MESSAGE" ||
  fail "não recebi o evento chamado-critico pelo SNS/SQS local"
pass "evento chamado-critico publicado e recebido via SNS/SQS"
aws_local sns unsubscribe --subscription-arn "$CRITICAL_SUBSCRIPTION_ARN" >/dev/null
CRITICAL_SUBSCRIPTION_ARN=""
aws_local sqs delete-queue --queue-url "$CRITICAL_QUEUE_URL" >/dev/null
CRITICAL_QUEUE_URL=""

api_request PATCH "/chamados/$CHAMADO_ID" '{"responsavelId":null}'
expect_status 200 "PATCH removendo responsável"
jq -e 'has("responsavelId") | not' >/dev/null <<<"$HTTP_BODY" ||
  fail "PATCH não removeu o responsável"

api_request GET /chamados/invalido
expect_status 400 "validação de UUID inválido"
api_request PATCH "/chamados/$CHAMADO_ID" '{"prioridade":"URGENTE"}'
expect_status 400 "validação de prioridade inválida"
api_request POST "/chamados/$CHAMADO_ID/comentarios" '{"texto":""}'
expect_status 400 "validação de comentário vazio"
api_request POST "/chamados/$CHAMADO_ID/anexos" \
  '{"nomeArquivo":"malware.exe","contentType":"application/x-msdownload","tamanho":10}'
expect_status 400 "validação de tipo de anexo não permitido"

if [[ "$WITH_DLQ_TEST" == true ]]; then
  ASSIGNMENT_QUEUE_URL="$(aws_local sqs get-queue-url \
    --queue-name atribuicao --query QueueUrl --output text)"
  DLQ_URL="$(aws_local sqs get-queue-url \
    --queue-name atribuicao-dlq --query QueueUrl --output text)"
  DLQ_BASELINE="$(aws_local sqs get-queue-attributes \
    --queue-url "$DLQ_URL" \
    --attribute-names ApproximateNumberOfMessages \
    --query Attributes.ApproximateNumberOfMessages \
    --output text)"
  [[ "$DLQ_BASELINE" =~ ^[0-9]+$ ]] ||
    fail "não foi possível ler a quantidade inicial de mensagens na DLQ"
  aws_local sqs send-message \
    --queue-url "$ASSIGNMENT_QUEUE_URL" \
    --message-body '{"eventType":"chamado-criado","chamadoId":"uuid-invalido"}' \
    >/dev/null

  DLQ_COUNT="$DLQ_BASELINE"
  for attempt in {1..50}; do
    DLQ_COUNT="$(aws_local sqs get-queue-attributes \
      --queue-url "$DLQ_URL" \
      --attribute-names ApproximateNumberOfMessages \
      --query Attributes.ApproximateNumberOfMessages \
      --output text)"
    if [[ "$DLQ_COUNT" =~ ^[0-9]+$ ]] && ((DLQ_COUNT > DLQ_BASELINE)); then
      break
    fi
    sleep 5
  done
  ((DLQ_COUNT > DLQ_BASELINE)) ||
    fail "a mensagem inválida não chegou à DLQ em até 250 s"
  pass "mensagem com falha repetida chegou à DLQ"

  ALARM_NAME="$(aws_local cloudformation list-stack-resources \
    --stack-name "$STACK_NAME" \
    --query "StackResourceSummaries[?LogicalResourceId=='AlarmeAtribuicaoDlq'].PhysicalResourceId | [0]" \
    --output text)"
  ALARM_STATE="$(aws_local cloudwatch describe-alarms \
    --alarm-names "$ALARM_NAME" \
    --query 'MetricAlarms[0].StateValue' \
    --output text)"
  printf 'Estado do alarme da DLQ (%s): %s\n' "$ALARM_NAME" "$ALARM_STATE"
  if [[ "$ALARM_STATE" != "ALARM" ]]; then
    printf 'AVISO: a métrica/avaliação de alarmes pode não estar totalmente simulada nesta versão do LocalStack.\n'
  fi
fi

save_state
printf '\nTestes concluídos. IDs e URL salvos em %s\n' "$STATE_FILE"
printf 'Para carregar em outro terminal: source %q\n' "$STATE_FILE"
if [[ "$WITH_DLQ_TEST" != true ]]; then
  printf 'Para incluir o teste de retry/DLQ (até ~250 s): bash scripts/test_localstack.sh --with-dlq\n'
fi
