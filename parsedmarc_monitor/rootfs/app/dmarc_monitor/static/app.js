"use strict";

const STRINGS = {
  de: {
    missing: "—",
    loading: "Lade Zustellungen …",
    requestError: "Die Zustellungen konnten nicht geladen werden.",
    validationError: "Bitte prüfen Sie den Datumsbereich.",
    details: "Diagnose anzeigen",
    detailsHide: "Diagnose ausblenden",
    passed: "Erfolgreich",
    failed: "Fehlerhaft",
    page: "Seite",
    of: "von",
    total: "Zustellgruppen",
    previous: "Zurück",
    next: "Weiter",
    interval: "Zeitraum",
    headerFrom: "Header-From",
    source: "Quelle",
    knownSource: "Bekannte Quelle",
    messages: "Nachrichten",
    results: "Ergebnisse",
    reportingOrg: "Reporting-Organisation",
    reportId: "Report-ID",
    policyDomain: "Policy-Domain",
    exactInterval: "Exaktes Intervall",
    envelopeFrom: "Envelope-From",
    disposition: "Disposition",
    dkimAlignment: "DKIM-Ausrichtung",
    spfAlignment: "SPF-Ausrichtung",
    sourceName: "Quellenname",
    asn: "ASN",
    asName: "AS-Name",
    country: "Land",
    baseDomain: "Basis-Domain",
    classification: "Klassifizierung",
    aligned: "ausgerichtet",
    notAligned: "nicht ausgerichtet",
  },
};

const ui = typeof document === "undefined" ? null : {
  form: document.getElementById("filters-form"),
  dateFrom: document.getElementById("date-from"),
  dateTo: document.getElementById("date-to"),
  outcome: document.getElementById("outcome"),
  search: document.getElementById("search"),
  body: document.getElementById("deliveries-body"),
  table: document.getElementById("delivery-table"),
  pagination: document.getElementById("pagination"),
  loading: document.getElementById("loading"),
  empty: document.getElementById("empty"),
  error: document.getElementById("error"),
  validationError: document.getElementById("validation-error"),
};

let activeFilters = null;

function localIsoDate(date) {
  const year = String(date.getFullYear()).padStart(4, "0");
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function defaultRange(now = new Date()) {
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const start = new Date(end);
  start.setDate(start.getDate() - 6);
  return { dateFrom: localIsoDate(start), dateTo: localIsoDate(end) };
}

function paginationState(payload, loading = false) {
  const total = Number(payload.total);
  const page = Number(payload.page);
  const pageSize = Number(payload.page_size);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  return {
    totalPages,
    previousDisabled: loading || page <= 1,
    nextDisabled: loading || total === 0 || page >= totalPages,
  };
}

function buildQuery(filters) {
  const params = new URLSearchParams();
  params.set("date_from", filters.dateFrom ?? filters.date_from);
  params.set("date_to", filters.dateTo ?? filters.date_to);
  params.set("outcome", filters.outcome || "all");
  if (filters.search) {
    params.set("search", filters.search);
  }
  params.set("page", String(filters.page || 1));
  params.set("page_size", String(filters.pageSize ?? filters.page_size ?? 50));
  return `./api/deliveries?${params.toString()}`;
}

function displayValue(value) {
  return value === null || value === undefined || value === "" ? STRINGS.de.missing : String(value);
}

function text(element, value) {
  element.textContent = "";
  element.append(document.createTextNode(displayValue(value)));
  return element;
}

function cell(value, label, className = "") {
  const element = document.createElement("td");
  element.dataset.label = label;
  if (className) {
    element.className = className;
  }
  text(element, value);
  return element;
}

function statusPill(value, passed) {
  const pill = document.createElement("span");
  pill.className = `status-pill ${passed ? "passed" : "failed"}`;
  text(pill, value);
  return pill;
}

function resultStack(item) {
  const stack = document.createElement("div");
  stack.className = "result-stack";
  stack.append(statusPill(item.dmarc_pass ? STRINGS.de.passed : STRINGS.de.failed, item.dmarc_pass));

  const dmarc = document.createElement("span");
  dmarc.className = "muted";
  text(dmarc, `DMARC: ${displayValue(item.dmarc_pass ? STRINGS.de.passed : STRINGS.de.failed)}`);
  stack.append(dmarc);

  const spf = document.createElement("span");
  spf.className = "muted";
  text(spf, `SPF: ${displayValue(item.spf_result)}`);
  stack.append(spf);

  const dkim = document.createElement("span");
  dkim.className = "muted";
  text(dkim, `DKIM: ${displayValue(item.dkim_result)}`);
  stack.append(dkim);
  return stack;
}

function detailLine(list, label, value) {
  const term = document.createElement("dt");
  text(term, label);
  const description = document.createElement("dd");
  text(description, value);
  list.append(term, description);
}

function detailsElement(item) {
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  text(summary, STRINGS.de.details);
  details.append(summary);

  const list = document.createElement("dl");
  list.className = "detail-list";
  detailLine(list, STRINGS.de.reportingOrg, item.reporting_org);
  detailLine(list, STRINGS.de.reportId, item.report_id);
  detailLine(list, STRINGS.de.policyDomain, item.policy_domain);
  detailLine(list, STRINGS.de.exactInterval, `${displayValue(item.interval_begin)} – ${displayValue(item.interval_end)}`);
  detailLine(list, STRINGS.de.envelopeFrom, item.envelope_from);
  detailLine(list, STRINGS.de.disposition, item.disposition);
  detailLine(list, STRINGS.de.dkimAlignment, item.dkim_aligned ? STRINGS.de.aligned : STRINGS.de.notAligned);
  detailLine(list, STRINGS.de.spfAlignment, item.spf_aligned ? STRINGS.de.aligned : STRINGS.de.notAligned);
  detailLine(list, STRINGS.de.sourceName, item.source_name);
  detailLine(list, STRINGS.de.asn, item.source_asn);
  detailLine(list, STRINGS.de.asName, item.source_as_name);
  detailLine(list, STRINGS.de.country, item.source_country);
  detailLine(list, STRINGS.de.baseDomain, item.source_base_domain);
  detailLine(list, STRINGS.de.classification, item.classification);
  details.append(list);
  return details;
}

function intervalText(item) {
  const reportDate = displayValue(item.report_date);
  return `${reportDate} · ${displayValue(item.interval_begin)} – ${displayValue(item.interval_end)}`;
}

function renderRows(items) {
  while (ui.body.firstChild) {
    ui.body.removeChild(ui.body.firstChild);
  }
  items.forEach((item) => {
    const row = document.createElement("tr");
    row.className = `delivery-row${item.dmarc_pass ? "" : " failed"}`;

    const interval = cell(null, STRINGS.de.interval, "interval-cell");
    text(interval, intervalText(item));
    interval.append(detailsElement(item));
    row.append(interval);

    row.append(cell(item.header_from, STRINGS.de.headerFrom));

    const source = cell(null, STRINGS.de.source, "source-stack");
    const sourceIp = document.createElement("span");
    text(sourceIp, item.source_ip);
    source.append(sourceIp);
    const reverseDns = document.createElement("span");
    reverseDns.className = "muted";
    text(reverseDns, item.source_reverse_dns);
    source.append(reverseDns);
    row.append(source);

    row.append(cell(item.known_source_name, STRINGS.de.knownSource));
    row.append(cell(item.message_count, STRINGS.de.messages));

    const results = cell(null, STRINGS.de.results);
    results.append(resultStack(item));
    row.append(results);
    ui.body.append(row);
  });
}

function renderPagination(payload) {
  while (ui.pagination.firstChild) {
    ui.pagination.removeChild(ui.pagination.firstChild);
  }
  const state = paginationState(payload);
  const totalPages = state.totalPages;
  const info = document.createElement("span");
  info.className = "pagination-info";
  text(info, `${STRINGS.de.page} ${payload.page} ${STRINGS.de.of} ${totalPages} · ${payload.total} ${STRINGS.de.total}`);

  const actions = document.createElement("span");
  actions.className = "pagination-actions";
  const previous = document.createElement("button");
  previous.type = "button";
  previous.id = "previous-page";
  text(previous, STRINGS.de.previous);
  previous.dataset.boundaryDisabled = String(state.previousDisabled);
  previous.disabled = state.previousDisabled;
  previous.addEventListener("click", () => loadDeliveries({ ...activeFilters, page: payload.page - 1 }));
  actions.append(previous);

  const next = document.createElement("button");
  next.type = "button";
  next.id = "next-page";
  text(next, STRINGS.de.next);
  next.dataset.boundaryDisabled = String(state.nextDisabled);
  next.disabled = state.nextDisabled;
  next.addEventListener("click", () => loadDeliveries({ ...activeFilters, page: payload.page + 1 }));
  actions.append(next);
  ui.pagination.append(info, actions);
}

function setFormDisabled(disabled) {
  Array.from(ui.form.elements).forEach((element) => {
    element.disabled = disabled;
  });
}

function setControlsDisabled(disabled) {
  setFormDisabled(disabled);
  ui.pagination.querySelectorAll("button").forEach((button) => {
    button.disabled = disabled || button.dataset.boundaryDisabled === "true";
  });
}

function setState(element, visible, value = null) {
  if (value !== null) {
    text(element, value);
  }
  element.hidden = !visible;
}

async function loadDeliveries(filters = activeFilters) {
  if (!filters) {
    return;
  }
  activeFilters = { ...filters };
  setControlsDisabled(true);
  setState(ui.loading, true, STRINGS.de.loading);
  setState(ui.error, false);
  setState(ui.validationError, false);
  try {
    const response = await fetch(buildQuery(activeFilters), { headers: { Accept: "application/json" } });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    renderRows(Array.isArray(payload.items) ? payload.items : []);
    renderPagination(payload);
    setState(ui.empty, payload.items.length === 0);
    ui.table.hidden = payload.items.length === 0;
  } catch (error) {
    setState(ui.error, true, STRINGS.de.requestError);
  } finally {
    setState(ui.loading, false);
    setControlsDisabled(false);
  }
}

function readFilters() {
  if (!ui.dateFrom.value || !ui.dateTo.value || ui.dateFrom.value > ui.dateTo.value) {
    setState(ui.validationError, true, STRINGS.de.validationError);
    return null;
  }
  return {
    dateFrom: ui.dateFrom.value,
    dateTo: ui.dateTo.value,
    outcome: ui.outcome.value,
    search: ui.search.value,
    page: 1,
    pageSize: 50,
  };
}

function init() {
  const range = defaultRange();
  ui.dateFrom.value = range.dateFrom;
  ui.dateTo.value = range.dateTo;
  activeFilters = { ...range, outcome: "all", search: "", page: 1, pageSize: 50 };
  ui.form.addEventListener("submit", (event) => {
    event.preventDefault();
    const filters = readFilters();
    if (filters) {
      loadDeliveries(filters);
    }
  });
  loadDeliveries(activeFilters);
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
}

const frontendApi = {
  localIsoDate,
  defaultRange,
  paginationState,
  buildQuery,
  renderRows,
  renderPagination,
  loadDeliveries,
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = frontendApi;
}

if (typeof globalThis !== "undefined") {
  Object.assign(globalThis, frontendApi);
}
