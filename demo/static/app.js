const ticketList = document.querySelector("#ticket-list");
const ticketDetail = document.querySelector("#ticket-detail");
const emptyDetail = document.querySelector("#empty-detail");
const notice = document.querySelector("#notice");
const filters = document.querySelector("#filters");
const loadMoreButton = document.querySelector("#load-more");
const newTicketDialog = document.querySelector("#new-ticket-dialog");
let selectedTicketId = null;
let nextToken = null;

const labels = {
  ABERTO: "Aberto",
  EM_ATENDIMENTO: "Em atendimento",
  RESOLVIDO: "Resolvido",
  FECHADO: "Fechado",
  BAIXA: "Baixa",
  MEDIA: "Média",
  ALTA: "Alta",
  CRITICA: "Crítica",
};

function showNotice(message, isError = false) {
  notice.textContent = message;
  notice.classList.toggle("notice-error", isError);
  notice.hidden = false;
  window.setTimeout(() => {
    notice.hidden = true;
  }, 5000);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : await response.blob();
  if (!response.ok) {
    throw new Error(body.erro || `A operação falhou (HTTP ${response.status})`);
  }
  return body;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("pt-BR");
}

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function showError(error) {
  showNotice(error.message || "Ocorreu um erro inesperado", true);
}

async function loadTickets(append = false) {
  const params = new URLSearchParams(new FormData(filters));
  params.set("limit", "20");
  if (append && nextToken) params.set("nextToken", nextToken);
  else {
    nextToken = null;
    ticketList.replaceChildren();
  }

  try {
    const data = await api(`/api/chamados?${params}`);
    nextToken = data.nextToken || null;
    loadMoreButton.hidden = !nextToken;
    if (!data.items.length && !append) {
      ticketList.append(makeElement("p", "empty-state", "Nenhum chamado encontrado."));
      return;
    }
    data.items.forEach((ticket) => {
      const button = makeElement("button", "ticket-card");
      button.type = "button";
      button.dataset.ticketId = ticket.id;
      button.classList.toggle("ticket-card-selected", ticket.id === selectedTicketId);
      button.addEventListener("click", () => selectTicket(ticket.id));

      const top = makeElement("div", "ticket-card-top");
      top.append(
        makeElement("span", "ticket-card-id", ticket.id.slice(0, 8)),
        makeElement("span", `mini-status status-${ticket.status}`, labels[ticket.status] || ticket.status),
      );
      button.append(top);
      button.append(makeElement("strong", "ticket-card-title", ticket.titulo || "Sem título"));
      button.append(makeElement("span", "ticket-card-date", formatDate(ticket.criadoEm)));
      ticketList.append(button);
    });
  } catch (error) {
    showError(error);
    if (!ticketList.childElementCount) {
      ticketList.append(makeElement("p", "empty-state", "Não foi possível carregar os chamados."));
    }
  }
}

async function selectTicket(ticketId) {
  selectedTicketId = ticketId;
  try {
    const ticket = await api(`/api/chamados/${encodeURIComponent(ticketId)}`);
    renderTicket(ticket);
    document.querySelectorAll(".ticket-card").forEach((card) => {
      card.classList.toggle("ticket-card-selected", card.dataset.ticketId === ticketId);
    });
  } catch (error) {
    showError(error);
  }
}

async function monitorAssignment(ticketId) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
    try {
      const ticket = await api(`/api/chamados/${encodeURIComponent(ticketId)}`);
      if (ticket.responsavelId) {
        if (selectedTicketId === ticketId) renderTicket(ticket);
        await loadTickets();
        showNotice(`Chamado atribuído a ${ticket.responsavelId}.`);
        return;
      }
    } catch (error) {
      showError(error);
      return;
    }
  }
  showNotice("A atribuição ainda está em processamento; atualize o chamado em instantes.");
}

function renderTicket(ticket) {
  emptyDetail.hidden = true;
  ticketDetail.hidden = false;
  document.querySelector("#ticket-id").textContent = `CHAMADO · ${ticket.id}`;
  document.querySelector("#ticket-title").textContent = ticket.titulo || "Sem título";
  document.querySelector("#ticket-description").textContent = ticket.descricao || "";
  document.querySelector("#ticket-category").textContent = `Categoria: ${ticket.categoria || "—"}`;
  document.querySelector("#ticket-created").textContent = `Criado em ${formatDate(ticket.criadoEm)}`;
  document.querySelector("#ticket-owner").textContent = `Responsável: ${ticket.responsavelId || "Não atribuído"}`;

  const status = document.querySelector("#ticket-status");
  status.textContent = labels[ticket.status] || ticket.status;
  status.className = `status status-${ticket.status}`;
  const priority = document.querySelector("#ticket-priority");
  priority.textContent = labels[ticket.prioridade] || ticket.prioridade;
  priority.className = `priority priority-${ticket.prioridade}`;

  const updateForm = document.querySelector("#update-form");
  updateForm.elements.status.value = ticket.status;
  updateForm.elements.prioridade.value = ticket.prioridade;
  updateForm.elements.responsavelId.value = ticket.responsavelId || "";

  const history = document.querySelector("#history");
  const entries = (ticket.historico || []).filter((entry) => entry.tipo !== "ANEXO");
  document.querySelector("#history-count").textContent = String(entries.length);
  history.replaceChildren();
  if (!entries.length) history.append(makeElement("p", "empty-state", "Sem eventos no histórico."));
  entries.slice().reverse().forEach((entry) => {
    const item = makeElement("article", "history-item");
    const marker = makeElement("span", "history-marker");
    const content = makeElement("div", "history-content");
    const title = entry.tipo === "COMENTARIO" ? "Comentário" : (entry.tipo || "Evento").replaceAll("_", " ");
    content.append(makeElement("strong", "", title));
    if (entry.texto) content.append(makeElement("p", "", entry.texto));
    if (entry.alteracoes) {
      const changes = Object.entries(entry.alteracoes)
        .map(([key, value]) => `${key}: ${value.de ?? "—"} → ${value.para ?? "—"}`)
        .join(" · ");
      content.append(makeElement("p", "history-changes", changes));
    }
    content.append(makeElement("time", "", formatDate(entry.criadoEm)));
    item.append(marker, content);
    history.append(item);
  });

  const attachmentList = document.querySelector("#attachments");
  attachmentList.replaceChildren();
  const attachments = (ticket.historico || []).filter((entry) => entry.anexoId);
  if (!attachments.length) {
    attachmentList.append(makeElement("p", "empty-state", "Nenhum anexo."));
  }
  attachments.forEach((attachment) => {
    const row = makeElement("div", "attachment-row");
    const info = makeElement("div", "attachment-info");
    info.append(
      makeElement("span", "attachment-icon", "↗"),
      makeElement("span", "", attachment.nomeArquivo || attachment.anexoId),
    );
    row.append(info);
    if (attachment.status === "CONFIRMADO") {
      const link = makeElement("a", "button button-quiet button-small", "Baixar");
      link.href = `/api/chamados/${encodeURIComponent(ticket.id)}/anexos/${encodeURIComponent(attachment.anexoId)}/arquivo`;
      row.append(link);
    } else {
      row.append(makeElement("span", "pending-label", "Pendente"));
    }
    attachmentList.append(row);
  });
}

function openCreateDialog() {
  newTicketDialog.showModal();
}

document.querySelector("#new-ticket-button").addEventListener("click", openCreateDialog);
document.querySelectorAll("[data-new-ticket]").forEach((button) => button.addEventListener("click", openCreateDialog));
document.querySelector("#close-dialog").addEventListener("click", () => newTicketDialog.close());
document.querySelector("#cancel-dialog").addEventListener("click", () => newTicketDialog.close());
filters.addEventListener("submit", (event) => {
  event.preventDefault();
  loadTickets();
});
loadMoreButton.addEventListener("click", () => loadTickets(true));

document.querySelector("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form));
  try {
    const created = await api("/api/chamados", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    form.reset();
    newTicketDialog.close();
    showNotice(`Chamado criado: ${created.id}. A atribuição é processada em segundo plano.`);
    await loadTickets();
    await selectTicket(created.id);
    void monitorAssignment(created.id);
  } catch (error) {
    showError(error);
  }
});

document.querySelector("#update-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    status: form.elements.status.value,
    prioridade: form.elements.prioridade.value,
  };
  const isNewCriticalPriority =
    payload.prioridade === "CRITICA" &&
    document.querySelector("#ticket-priority").textContent !== labels.CRITICA;
  if (form.elements.responsavelId.value.trim()) {
    payload.responsavelId = form.elements.responsavelId.value.trim();
  }
  try {
    await api(`/api/chamados/${encodeURIComponent(selectedTicketId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    showNotice(
      isNewCriticalPriority
        ? "Prioridade crítica salva; o evento de alerta será processado."
        : "Chamado atualizado.",
    );
    await loadTickets();
    await selectTicket(selectedTicketId);
  } catch (error) {
    showError(error);
  }
});

document.querySelector("#clear-owner").addEventListener("click", async () => {
  if (!selectedTicketId) return;
  try {
    await api(`/api/chamados/${encodeURIComponent(selectedTicketId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ responsavelId: null }),
    });
    showNotice("Responsável removido.");
    await loadTickets();
    await selectTicket(selectedTicketId);
  } catch (error) {
    showError(error);
  }
});

document.querySelector("#comment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api(`/api/chamados/${encodeURIComponent(selectedTicketId)}/comentarios`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texto: form.elements.texto.value }),
    });
    form.reset();
    showNotice("Comentário adicionado ao histórico.");
    await selectTicket(selectedTicketId);
  } catch (error) {
    showError(error);
  }
});

const attachmentInput = document.querySelector("#attachment-form input[type=file]");
const attachmentLabel = document.querySelector("#attachment-file-label");
const uploadButton = document.querySelector("#upload-button");
const attachmentTypes = {
  pdf: "application/pdf",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  txt: "text/plain",
};

attachmentInput.addEventListener("change", () => {
  const file = attachmentInput.files[0];
  if (!file) {
    attachmentLabel.textContent = "Escolher PDF, imagem ou texto (até 10 MiB)";
    return;
  }
  const extension = file.name.split(".").pop().toLowerCase();
  const contentType = attachmentTypes[extension];
  const size = `${(file.size / (1024 * 1024)).toFixed(2)} MiB`;
  attachmentLabel.textContent = `${file.name} · ${size}`;

  if (!contentType) {
    showNotice("Formato não permitido. Escolha PDF, JPG, PNG ou TXT.", true);
  } else if (file.size > 10 * 1024 * 1024) {
    showNotice("O arquivo excede o limite de 10 MiB.", true);
  }
});

document.querySelector("#attachment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const file = attachmentInput.files[0];
  if (!file) {
    showNotice("Selecione um arquivo antes de enviar.", true);
    return;
  }
  const extension = file.name.split(".").pop().toLowerCase();
  const contentType = attachmentTypes[extension];
  if (!contentType || file.size === 0 || file.size > 10 * 1024 * 1024) {
    showNotice("Escolha um PDF, JPG, PNG ou TXT com tamanho entre 1 byte e 10 MiB.", true);
    return;
  }
  const formData = new FormData(form);
  formData.set("arquivo", new File([file], file.name, { type: contentType }));
  uploadButton.disabled = true;
  uploadButton.textContent = "Enviando…";
  try {
    const result = await api(`/api/chamados/${encodeURIComponent(selectedTicketId)}/anexos`, {
      method: "POST",
      body: formData,
    });
    form.reset();
    attachmentLabel.textContent = "Escolher PDF, imagem ou texto (até 10 MiB)";
    showNotice(`Anexo ${result.status === "CONFIRMADO" ? "enviado e confirmado" : "processado"}.`);
    await selectTicket(selectedTicketId);
  } catch (error) {
    showError(error);
  } finally {
    uploadButton.disabled = false;
    uploadButton.textContent = "Enviar anexo";
  }
});

loadTickets();
