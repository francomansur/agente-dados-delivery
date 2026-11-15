// Page to try the agent. Each question becomes a card with the answer, the data and, on request,
// the technical details. There is no conversation memory: every question goes alone to the API.
"use strict";

const ENDPOINT = "/questions?include_details=true";
const IDLE_PLACEHOLDER = "Pergunte sobre pedidos, restaurantes, entregas…";
const BUSY_PLACEHOLDER = "Consultando os dados…";

const STATUS_LABELS = {
  answered: "respondida",
  refused: "recusada",
  failed: "falhou",
};

const ERROR_MESSAGES = {
  422: "A pergunta precisa ter entre 3 e 500 caracteres.",
  502: "O modelo de linguagem não respondeu agora. Tente de novo em instantes.",
  503: "O banco de dados está fora do ar agora. Tente de novo em instantes.",
};
const MISSING_KEY_MESSAGE =
  "A chave da API da Anthropic não está configurada. Defina ANTHROPIC_API_KEY no .env e reinicie a api.";
const NETWORK_MESSAGE = "Não consegui falar com a API. Confira se ela está no ar e tente de novo.";

// Columns whose whole numbers are codes or dates, not quantities: shown without thousands separator.
const PLAIN_NUMBER_COLUMN = /(^|_)(id|year|month|day|dow|hour)$/;
// Dates and timestamps as the API sends them, such as 2026-01-01 or 2026-01-01 11:16:59.
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(?:[+-]\d{2}:?\d{2}|Z)?)?$/;

const integerFormat = new Intl.NumberFormat("pt-BR");
const decimalFormat = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const secondsFormat = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const costFormat = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 4, maximumFractionDigits: 4 });

const form = document.getElementById("question-form");
const input = document.getElementById("question-input");
const sendButton = form.querySelector(".composer__send");
const exampleButtons = document.querySelectorAll(".example");
const timeline = document.getElementById("timeline");
const template = document.getElementById("card-template");
const statusRegion = document.getElementById("status");
let cardCount = 0;

form.addEventListener("submit", (event) => {
  event.preventDefault();
  ask(input.value.trim());
});

for (const button of exampleButtons) {
  button.addEventListener("click", () => ask(button.textContent.trim()));
}

async function ask(question) {
  if (input.disabled || question === "") {
    return;
  }

  const card = createCard(question);
  setBusy(true);

  try {
    const response = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });

    if (response.ok) {
      showAnswer(card, await response.json());
    } else {
      showError(card, await errorMessage(response));
    }
  } catch {
    showError(card, NETWORK_MESSAGE);
  } finally {
    setBusy(false);
  }
}

function createCard(question) {
  cardCount += 1;
  const card = template.content.firstElementChild.cloneNode(true);
  card.querySelector(".card__question").textContent = question;
  setBadge(card, "pending", "consultando");

  const answer = card.querySelector(".card__answer");
  answer.textContent = BUSY_PLACEHOLDER;
  answer.classList.add("is-pending");

  const details = card.querySelector(".card__details");
  details.id = `details-${cardCount}`;
  card.querySelector(".card__toggle").setAttribute("aria-controls", details.id);

  timeline.append(card);
  card.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "nearest" });
  statusRegion.textContent = BUSY_PLACEHOLDER;

  return card;
}

function setBusy(busy) {
  input.disabled = busy;
  sendButton.disabled = busy;

  for (const button of exampleButtons) {
    button.disabled = busy;
  }

  if (busy) {
    input.value = "";
    input.placeholder = BUSY_PLACEHOLDER;
    sendButton.textContent = "Consultando…";

    return;
  }

  input.placeholder = IDLE_PLACEHOLDER;
  sendButton.textContent = "Perguntar";
  input.focus();
}

function setBadge(card, kind, label) {
  const badge = card.querySelector(".badge");
  badge.className = `badge badge--${kind}`;
  badge.textContent = label;
}

function showAnswer(card, data) {
  const label = STATUS_LABELS[data.status] ?? data.status;
  setBadge(card, data.status, label);

  const answer = card.querySelector(".card__answer");
  answer.classList.remove("is-pending");
  answer.textContent = data.answer;

  if (data.columns.length > 0) {
    fillTable(card, data.columns, data.rows, data.truncated);
  }

  fillDetails(card, data);
  statusRegion.textContent = `Pergunta ${label}.`;
}

function showError(card, message) {
  setBadge(card, "error", "erro");

  const answer = card.querySelector(".card__answer");
  answer.classList.remove("is-pending");
  answer.classList.add("is-error");
  answer.textContent = message;
  statusRegion.textContent = message;
}

async function errorMessage(response) {
  if (response.status === 503 && (await readDetail(response)).includes("ANTHROPIC_API_KEY")) {
    return MISSING_KEY_MESSAGE;
  }

  return ERROR_MESSAGES[response.status] ?? `Algo deu errado (erro ${response.status}). Tente de novo.`;
}

async function readDetail(response) {
  try {
    const body = await response.json();

    return typeof body.detail === "string" ? body.detail : "";
  } catch {
    return "";
  }
}

function fillTable(card, columns, rows, truncated) {
  const numeric = columns.map((_, index) => rows.some((row) => typeof row[index] === "number"));
  // A column with one decimal value shows every number with 2 decimals: 5 becomes 5,00 next to 6,73.
  const decimal = columns.map((_, index) =>
    rows.some((row) => typeof row[index] === "number" && !Number.isInteger(row[index])),
  );
  const headerRow = card.querySelector("thead tr");

  columns.forEach((column, index) => {
    const header = document.createElement("th");
    header.scope = "col";
    header.textContent = column;
    header.classList.toggle("is-number", numeric[index]);
    headerRow.append(header);
  });

  const body = card.querySelector("tbody");

  for (const row of rows) {
    const line = document.createElement("tr");

    row.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = formatCell(value, columns[index], decimal[index]);
      cell.classList.toggle("is-number", numeric[index]);
      line.append(cell);
    });

    body.append(line);
  }

  const note = card.querySelector(".card__note");

  if (rows.length === 0) {
    note.textContent = "A consulta não trouxe nenhuma linha.";
    note.hidden = false;
  } else if (truncated) {
    note.textContent = `Mostrando só as primeiras ${integerFormat.format(rows.length)} linhas.`;
    note.hidden = false;
  }

  card.querySelector(".card__data").hidden = false;
}

function formatCell(value, column, decimalColumn) {
  if (value === null) {
    return "—";
  }

  if (typeof value === "boolean") {
    return value ? "sim" : "não";
  }

  if (typeof value === "number") {
    if (decimalColumn) {
      return decimalFormat.format(value);
    }

    return PLAIN_NUMBER_COLUMN.test(column) ? String(value) : integerFormat.format(value);
  }

  const date = ISO_DATE.exec(value);

  if (date === null) {
    return value;
  }

  const [, year, month, day, hours, minutes] = date;
  const time = hours === undefined ? "" : ` ${hours}:${minutes}`;

  return `${day}/${month}/${year}${time}`;
}

function fillDetails(card, data) {
  const code = card.querySelector(".sql__code code");
  const copyButton = card.querySelector(".sql__copy");
  card.querySelector(".sql__label").textContent =
    data.status === "failed" ? "Último SQL tentado" : "SQL executado";

  if (data.sql) {
    code.textContent = data.sql;
    copyButton.addEventListener("click", () => copySql(copyButton, data.sql));
  } else {
    code.textContent = "Nenhum SQL foi executado.";
    copyButton.hidden = true;
  }

  card.querySelector(".stats__attempts").textContent = integerFormat.format(data.attempts);
  card.querySelector(".stats__time").textContent = `${secondsFormat.format(data.duration_ms / 1000)} s`;
  card.querySelector(".stats__cost").textContent = `US$ ${costFormat.format(data.usage.cost_usd)}`;

  const toggle = card.querySelector(".card__toggle");
  const details = card.querySelector(".card__details");
  toggle.hidden = false;

  toggle.addEventListener("click", () => {
    const open = details.hidden;
    details.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? "Ocultar detalhes" : "Ver detalhes";
  });
}

async function copySql(button, sql) {
  try {
    await navigator.clipboard.writeText(sql);
    button.textContent = "Copiado!";
  } catch {
    button.textContent = "Não deu para copiar";
  }

  setTimeout(() => {
    button.textContent = "Copiar SQL";
  }, 2000);
}

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
