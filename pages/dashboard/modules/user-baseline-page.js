import { esc, formatTimestamp } from "./utils.js";

const CATEGORIES = [
  "address_identity",
  "relationship",
  "interaction_preference",
  "long_term_boundary",
  "global_constraint",
];
const ENTRY_SECTIONS = [
  {
    key: "identity",
    categories: new Set(["address_identity", "relationship"]),
  },
  {
    key: "interaction",
    categories: new Set(["interaction_preference"]),
  },
  {
    key: "boundaries",
    categories: new Set(["long_term_boundary", "global_constraint"]),
  },
];
const WARNING_DISMISSED_AT_KEY = "livingmemory.baselineWarningDismissedAt";

export class UserBaselinePage {
  constructor(state, apiClient, peekPanel) {
    this.state = state;
    this.api = apiClient;
    this.peek = peekPanel;
    this.items = [];
    this.total = 0;
    this.page = 1;
    this.pageSize = 20;
    this.keyword = "";
    this.batchWindows = 8;
    this.generationWarning = null;
    this.generationWarningAt = 0;
    this.detail = null;
    this.editing = false;
    this._fetchGeneration = 0;
    this._refreshTimer = null;
  }

  initEventListeners() {
    const keyword = document.getElementById("baseline-keyword");
    const pageSize = document.getElementById("baseline-page-size");
    const refresh = document.getElementById("baseline-refresh");
    const prev = document.getElementById("baseline-prev");
    const next = document.getElementById("baseline-next");
    const warningDismiss = document.getElementById("baseline-warning-dismiss");

    if (keyword) keyword.addEventListener("keydown", event => {
      if (event.key !== "Enter") return;
      this.keyword = keyword.value.trim();
      this.page = 1;
      this.fetch();
    });
    if (pageSize) pageSize.addEventListener("change", () => {
      this.pageSize = Number(pageSize.value) || 20;
      this.page = 1;
      this.fetch();
    });
    if (refresh) refresh.addEventListener("click", () => this.fetch());
    if (prev) prev.addEventListener("click", () => {
      if (this.page <= 1) return;
      this.page -= 1;
      this.fetch();
    });
    if (next) next.addEventListener("click", () => {
      if (this.page >= this.totalPages()) return;
      this.page += 1;
      this.fetch();
    });
    if (warningDismiss) warningDismiss.addEventListener("click", () => {
      try {
        window.localStorage?.setItem(
          WARNING_DISMISSED_AT_KEY,
          String(this.generationWarningAt),
        );
      } catch (_) { /* ignore */ }
      this.renderGenerationWarning();
    });

    this._refreshTimer = window.setInterval(() => {
      if (this.state.page === "baselines" && !this.state.isEditing) this.fetch({ quiet: true });
    }, 60000);
  }

  totalPages() {
    return Math.max(1, Math.ceil(this.total / this.pageSize));
  }

  async fetch(options = {}) {
    const generation = ++this._fetchGeneration;
    try {
      const data = await this.api.get("user-baselines", {
        keyword: this.keyword,
        page: String(this.page),
        page_size: String(this.pageSize),
      });
      if (generation !== this._fetchGeneration) return;
      this.items = Array.isArray(data.items) ? data.items : [];
      this.total = Number(data.total || 0);
      this.batchWindows = Number(data.batch_windows || 8);
      this.generationWarning = data.generation_warning && typeof data.generation_warning === "object"
        ? data.generation_warning
        : null;
      this.generationWarningAt = Number(this.generationWarning?.occurred_at || 0);
      this.render();
    } catch (error) {
      if (generation !== this._fetchGeneration || options.quiet) return;
      this.toast(error.message || window.t("baseline.fetchFailed"), true);
    }
  }

  render() {
    const container = document.getElementById("baseline-cards");
    if (!container) return;
    if (!this.items.length) {
      container.innerHTML = '<div class="table-empty">' + esc(window.t("baseline.empty")) + "</div>";
    } else {
      container.innerHTML = this.items.map(item => this.renderCard(item)).join("");
      container.querySelectorAll(".baseline-card[data-user-id]").forEach(card => {
        card.addEventListener("click", () => this.openDetail(Number(card.dataset.userId)));
      });
    }
    this.updatePagination();
    this.renderGenerationWarning();
    if (window.lmHydrateIcons) window.lmHydrateIcons();
  }

  shouldShowGenerationWarning() {
    if (!this.generationWarningAt) return false;
    try {
      const dismissedAt = Number(
        window.localStorage?.getItem(WARNING_DISMISSED_AT_KEY) || 0,
      );
      return dismissedAt < this.generationWarningAt;
    } catch (_) {
      return true;
    }
  }

  renderGenerationWarning() {
    const warning = document.getElementById("baseline-generation-warning");
    if (!warning) return;
    warning.classList.toggle("hidden", !this.shouldShowGenerationWarning());
    const message = document.getElementById("baseline-generation-warning-text");
    if (!message || !this.generationWarning) return;
    message.textContent = this.generationWarningText();
  }

  generationWarningText() {
    const reasons = Array.isArray(this.generationWarning.reasons)
      ? this.generationWarning.reasons
      : [];
    return window.t(
      "baseline.generationWarning",
      reasons[0] || window.t("baseline.unknownFailureReason"),
      reasons[1] || window.t("baseline.unknownFailureReason"),
    );
  }

  renderCard(item) {
    const cooldown = this.cooldownText(item.cooldown_remaining);
    const last = item.last_success_at
      ? window.t("baseline.lastGenerated", formatTimestamp(item.last_success_at))
      : window.t("baseline.neverGenerated");
    return '<button type="button" class="baseline-card" data-user-id="' + Number(item.user_id) + '">' +
      '<div class="baseline-card-head"><div class="baseline-card-name">' + esc(item.display_name || item.canonical_identity) + '</div>' +
      '<span class="type-tag">' + esc(item.platform || "unknown") + '</span></div>' +
      '<div class="baseline-card-identity">' + esc(item.platform + ":" + item.canonical_identity) + '</div>' +
      '<div class="baseline-card-stats">' +
      this.cardStat(item.persona_count || 0, "baseline.personas") +
      this.cardStat(item.entry_count || 0, "baseline.entries") +
      this.cardStat((item.progress_count || 0) + "/" + this.batchWindows, "baseline.progress") +
      '</div><div class="baseline-card-foot"><span>' + esc(last) + '</span><span>' + esc(cooldown) + '</span></div></button>';
  }

  cardStat(value, key) {
    return '<div class="baseline-card-stat"><strong>' + esc(value) + '</strong><span>' + esc(window.t(key)) + "</span></div>";
  }

  cooldownText(seconds) {
    const remaining = Math.max(0, Number(seconds || 0));
    if (!remaining) return window.t("baseline.ready");
    const hours = Math.floor(remaining / 3600);
    const minutes = Math.ceil((remaining % 3600) / 60);
    return window.t("baseline.cooldown", hours, minutes);
  }

  updatePagination() {
    const totalPages = this.totalPages();
    const info = document.getElementById("baseline-pagination-info");
    const prev = document.getElementById("baseline-prev");
    const next = document.getElementById("baseline-next");
    if (info) info.textContent = window.t("common.page", this.page, totalPages, this.total);
    if (prev) prev.disabled = this.page <= 1;
    if (next) next.disabled = this.page >= totalPages;
  }

  async openDetail(userId) {
    try {
      this.detail = await this.api.get("user-baselines/detail", { user_id: String(userId) });
      this.renderDetail();
    } catch (error) {
      this.toast(error.message || window.t("baseline.detailFailed"), true);
    }
  }

  renderDetail() {
    if (!this.detail) return;
    this.editing = false;
    this.state.isEditing = false;
    document.getElementById("peek-badge").innerHTML = '<span class="peek-node-badge person">' + esc(window.t("baseline.badge")) + "</span>";
    document.getElementById("peek-title").textContent = this.detail.display_name || this.detail.canonical_identity;

    let html = '<div class="memory-detail-actions">';
    html += '<button class="btn btn-primary btn-sm" data-baseline-action="add"><i data-lucide="plus" aria-hidden="true"></i><span>' + esc(window.t("baseline.add")) + "</span></button>";
    html += "</div>";

    const groups = this.groupEntries(this.detail.entries || []);
    if (!groups.length) {
      html += '<div class="table-empty">' + esc(window.t("baseline.noEntries")) + "</div>";
    } else {
      for (const group of groups) {
        html += '<div class="baseline-group-title">' + esc(group.label) + "</div>";
        html += '<div class="baseline-entry-sections">';
        for (const section of this.groupEntrySections(group.entries)) {
          html += '<section class="baseline-entry-section">';
          html += '<div class="baseline-entry-section-head"><span>' + esc(section.label) + '</span><span class="baseline-entry-section-count">' + section.entries.length + "</span></div>";
          html += '<div class="baseline-entry-list">' + section.entries.map(entry => this.renderEntry(entry)).join("") + "</div></section>";
        }
        html += "</div>";
      }
    }
    html += '<div class="baseline-danger-zone"><button class="btn btn-danger btn-sm" data-baseline-action="delete-user"><i data-lucide="user-round-x" aria-hidden="true"></i><span>' + esc(window.t("baseline.deleteUser")) + "</span></button></div>";
    const body = document.getElementById("peek-body");
    body.innerHTML = html;
    body.onclick = event => this.handleDetailAction(event);
    this.peek.open(true);
    if (window.lmHydrateIcons) window.lmHydrateIcons();
  }

  groupEntries(entries) {
    const groups = new Map();
    for (const entry of entries) {
      const persona = entry.persona_id || "";
      if (!groups.has(persona)) {
        groups.set(persona, { label: persona || window.t("baseline.global"), entries: [] });
      }
      groups.get(persona).entries.push(entry);
    }
    return Array.from(groups.values());
  }

  groupEntrySections(entries) {
    return ENTRY_SECTIONS.map(section => ({
      label: window.t("baseline.section." + section.key),
      entries: entries.filter(entry => section.categories.has(entry.category)),
    })).filter(section => section.entries.length);
  }

  renderEntry(entry) {
    const state = entry.enabled ? window.t("common.enabled") : window.t("common.disabled");
    const source = entry.source_type === "manual" ? window.t("baseline.manual") : window.t("baseline.automatic");
    const autoUpdate = entry.allow_auto_update ? window.t("baseline.autoUpdateAllowed") : window.t("baseline.autoUpdateBlocked");
    let html = '<article class="baseline-entry-card ' + (entry.enabled ? "" : "is-disabled") + '">';
    html += '<div class="baseline-entry-head"><span class="type-tag">' + esc(window.t("baseline.category." + entry.category)) + '</span><span class="status-pill ' + (entry.enabled ? "active" : "") + '">' + esc(state) + "</span></div>";
    html += '<div class="baseline-entry-content">' + esc(entry.content) + "</div>";
    html += '<div class="baseline-entry-meta">' + esc(source + " · " + autoUpdate) + "</div>";
    if (Array.isArray(entry.evidence) && entry.evidence.length) {
      html += '<details class="memory-fact-technical"><summary>' + esc(window.t("baseline.evidence", entry.evidence.length)) + '</summary><div class="memory-fact-lifecycle">' + entry.evidence.map(ev => this.renderEvidence(ev)).join("") + "</div></details>";
    }
    html += '<div class="baseline-entry-actions">';
    html += this.actionButton("edit", entry.entry_id, "pencil", "baseline.edit");
    html += this.actionButton("delete-entry", entry.entry_id, "trash-2", "baseline.delete", true);
    html += "</div></article>";
    return html;
  }

  renderEvidence(ev) {
    if (!ev || typeof ev !== "object") {
      return '<div class="memory-fact-lifecycle-item"><span class="evidence-id">' + esc(String(ev)) + "</span></div>";
    }
    const role = String(ev.role || "unknown");
    const speaker = ev.speaker_name || (role === "assistant" ? "Bot" : window.t("baseline.unknownUser"));
    const time = ev.timestamp ? formatTimestamp(ev.timestamp) : "";
    const title = time ? ' title="' + esc(time) + '"' : "";
    const quote = ev.text ? "：“" + esc(ev.text) + "”" : "";
    return '<div class="memory-fact-lifecycle-item"' + title + '><span class="evidence-id">' + esc(ev.id) + "</span> — " + esc(speaker) + quote + "</div>";
  }

  actionButton(action, entryId, icon, key, danger = false) {
    return '<button class="btn btn-sm ' + (danger ? "btn-danger" : "btn-secondary") + '" data-baseline-action="' + action + '" data-entry-id="' + esc(entryId) + '"><i data-lucide="' + icon + '" aria-hidden="true"></i><span>' + esc(window.t(key)) + "</span></button>";
  }

  findEntry(entryId) {
    return (this.detail?.entries || []).find(item => item.entry_id === entryId) || null;
  }

  async handleDetailAction(event) {
    const button = event.target.closest("[data-baseline-action]");
    if (!button) return;
    const action = button.dataset.baselineAction;
    const entry = this.findEntry(button.dataset.entryId);
    if (action === "add") return this.renderForm(null);
    if (action === "edit" && entry) return this.renderForm(entry);
    if (action === "delete-entry" && entry) return this.deleteEntry(entry);
    if (action === "delete-user") return this.deleteUser();
  }

  renderForm(entry) {
    this.editing = true;
    this.state.isEditing = true;
    const personas = new Set(this.detail.personas || []);
    if (entry?.persona_id) personas.add(entry.persona_id);
    let html = '<div class="confirm-dialog-title">' + esc(window.t(entry ? "baseline.editTitle" : "baseline.addTitle")) + "</div>";
    html += '<form class="baseline-form" id="baseline-entry-form">';
    html += this.selectField("baseline-entry-category", "baseline.categoryLabel", CATEGORIES.map(category => [category, window.t("baseline.category." + category)]), entry?.category || CATEGORIES[0]);
    html += this.selectField("baseline-entry-persona", "baseline.scopeLabel", [["", window.t("baseline.global")], ...Array.from(personas).map(persona => [persona, persona])], entry?.persona_id || "");
    html += '<div class="baseline-form-field"><label for="baseline-entry-content">' + esc(window.t("baseline.contentLabel")) + '</label><textarea id="baseline-entry-content" class="memory-detail-edit-area compact" maxlength="180">' + esc(entry?.content || "") + "</textarea></div>";
    html += '<label class="baseline-form-check"><input id="baseline-entry-enabled" type="checkbox" ' + (entry?.enabled === false ? "" : "checked") + ' />' + esc(window.t("baseline.enabledLabel")) + "</label>";
    html += '<label class="baseline-form-check"><input id="baseline-entry-auto-update" type="checkbox" ' + (entry?.allow_auto_update ? "checked" : "") + ' />' + esc(window.t("baseline.allowAutoUpdate")) + "</label>";
    html += '<div class="baseline-form-help" id="baseline-global-note">' + esc(window.t("baseline.globalManualOnly")) + "</div>";
    html += '<div class="confirm-dialog-actions"><button type="button" class="btn btn-secondary" id="baseline-form-cancel">' + esc(window.t("common.cancel")) + '</button><button type="submit" class="btn btn-primary">' + esc(window.t("common.save")) + "</button></div></form>";
    const body = document.getElementById("peek-body");
    body.innerHTML = html;
    body.onclick = null;
    const persona = document.getElementById("baseline-entry-persona");
    const autoUpdate = document.getElementById("baseline-entry-auto-update");
    const disableAutoUpdate = () => { autoUpdate.checked = false; };
    document.getElementById("baseline-entry-category").addEventListener("change", disableAutoUpdate);
    document.getElementById("baseline-entry-content").addEventListener("input", disableAutoUpdate);
    persona.addEventListener("change", () => {
      disableAutoUpdate();
      this.updateAutoUpdateState();
    });
    this.updateAutoUpdateState();
    document.getElementById("baseline-form-cancel").addEventListener("click", () => this.renderDetail());
    document.getElementById("baseline-entry-form").addEventListener("submit", event => {
      event.preventDefault();
      this.saveForm(entry);
    });
  }

  selectField(id, labelKey, options, selected) {
    return '<div class="baseline-form-field"><label for="' + id + '">' + esc(window.t(labelKey)) + '</label><select class="input select" id="' + id + '">' + options.map(([value, label]) => '<option value="' + esc(value) + '" ' + (value === selected ? "selected" : "") + '>' + esc(label) + "</option>").join("") + "</select></div>";
  }

  updateAutoUpdateState() {
    const persona = document.getElementById("baseline-entry-persona");
    const autoUpdate = document.getElementById("baseline-entry-auto-update");
    const note = document.getElementById("baseline-global-note");
    if (!persona || !autoUpdate || !note) return;
    const isGlobal = !persona.value;
    if (isGlobal) autoUpdate.checked = false;
    autoUpdate.disabled = isGlobal;
    note.classList.toggle("hidden", !isGlobal);
  }

  async saveForm(entry) {
    const content = document.getElementById("baseline-entry-content").value.trim();
    if (!content) return this.toast(window.t("baseline.contentRequired"), true);
    const payload = {
      user_id: this.detail.user_id,
      category: document.getElementById("baseline-entry-category").value,
      persona_id: document.getElementById("baseline-entry-persona").value,
      content,
      enabled: document.getElementById("baseline-entry-enabled").checked,
      allow_auto_update: document.getElementById("baseline-entry-auto-update").checked,
    };
    if (entry) Object.assign(payload, { entry_id: entry.entry_id, revision: entry.revision });
    await this.postEntry(payload);
  }

  async postEntry(payload) {
    try {
      this.detail = await this.api.post("user-baselines/entries/upsert", payload);
      this.toast(window.t("baseline.saved"));
      this.renderDetail();
      this.fetch({ quiet: true });
    } catch (error) {
      this.toast(error.message || window.t("baseline.saveFailed"), true);
    }
  }

  async deleteEntry(entry) {
    const confirmed = await this.peek.showConfirmDialog(window.t("baseline.deleteTitle"), window.t("baseline.deleteMessage"));
    if (!confirmed) return this.renderDetail();
    try {
      this.detail = await this.api.post("user-baselines/entries/delete", {
        user_id: this.detail.user_id,
        entry_id: entry.entry_id,
        revision: entry.revision,
      });
      this.toast(window.t("baseline.deleted"));
      this.renderDetail();
      this.fetch({ quiet: true });
    } catch (error) {
      this.toast(error.message || window.t("baseline.deleteFailed"), true);
      this.renderDetail();
    }
  }

  async deleteUser() {
    const confirmed = await this.peek.showConfirmDialog(window.t("baseline.deleteUserTitle"), window.t("baseline.deleteUserMessage"));
    if (!confirmed) return this.renderDetail();
    try {
      await this.api.post("user-baselines/delete", {
        user_id: this.detail.user_id,
        revision: this.detail.revision,
        confirm: true,
      });
      this.detail = null;
      this.peek.close();
      this.toast(window.t("baseline.userDeleted"));
      this.fetch();
    } catch (error) {
      this.toast(error.message || window.t("baseline.deleteFailed"), true);
      this.renderDetail();
    }
  }

  refreshI18n() {
    this.render();
    if (this.detail && !this.state.isEditing) this.renderDetail();
  }

  toast(message, isError = false) {
    if (window.lmShowToast) window.lmShowToast(message, isError);
  }
}
