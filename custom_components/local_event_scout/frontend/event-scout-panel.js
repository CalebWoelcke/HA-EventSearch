// Event Scout sidebar panel. Plain web component, no build step.
// hass.callApi prefixes "/api/", so paths below are relative to it.

const API = "local_event_scout";
const POLL_MS = 2500;

const esc = (value = "") =>
  String(value ?? "").replace(/[&<>'"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[c]);

function errorText(err) {
  if (!err) return "Unknown error";
  if (typeof err === "string") return err;
  if (err.body) {
    if (typeof err.body === "string") return err.body;
    if (err.body.message) return err.body.message;
  }
  if (err.message) return err.message;
  if (err.error) return err.error;
  try { return JSON.stringify(err); } catch (_) { return String(err); }
}

function parseStart(value) {
  if (!value) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [y, m, d] = value.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  const date = new Date(value);
  return isNaN(date) ? null : date;
}

function dayKey(date) {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function dayLabel(date) {
  const today = new Date();
  const tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
  if (dayKey(date) === dayKey(today)) return "Today";
  if (dayKey(date) === dayKey(tomorrow)) return "Tomorrow";
  return date.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}

function whenText(event) {
  const start = parseStart(event.start);
  if (!start) return esc(event.start);
  const day = start.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  let text = event.all_day ? day : `${day}, ${start.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  if (event.end && event.end.slice(0, 10) !== event.start.slice(0, 10)) {
    const end = parseStart(event.end.slice(0, 10));
    if (end) text += ` – ${end.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
  }
  return text;
}

function relative(iso) {
  if (!iso) return "";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

const money = (value) => (typeof value === "number" ? `$${value.toFixed(2)}` : "–");

class LocalEventScoutPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "events";
    this._filter = "picks";
    this._sort = "score";
    this._dirty = false;
    this._error = "";
    this._notice = "";
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._menu) this._menu.hass = hass;
    if (first) this._load();
  }

  set narrow(narrow) {
    this._narrow = narrow;
    this.toggleAttribute("narrow", !!narrow);
    if (this._menu) {
      this._menu.narrow = narrow;
      this._menu.style.display = narrow ? "" : "none";
    }
  }

  set panel(_panel) {}

  disconnectedCallback() {
    clearInterval(this._poll);
    this._poll = null;
  }

  connectedCallback() {
    if (this._state && this._state.scanning) this._startPolling();
  }

  async _api(method, path, body) {
    try {
      return await this._hass.callApi(method, `${API}/${path}`, body);
    } catch (err) {
      throw new Error(errorText(err));
    }
  }

  async _load() {
    this._renderShell();
    try {
      this._setState(await this._api("GET", "config"), true);
    } catch (err) {
      this._error = `Could not load Event Scout: ${err.message}`;
      this._renderHeader();
    }
  }

  _setState(state, resetDraft = false) {
    this._state = state;
    if (resetDraft || !this._dirty) {
      this._draft = JSON.parse(JSON.stringify(state.config));
      this._dirty = false;
    }
    this._renderHeader();
    this._renderBody();
    if (state.scanning) this._startPolling();
  }

  _startPolling() {
    if (this._poll) return;
    this._poll = setInterval(async () => {
      try {
        const state = await this._api("GET", "config");
        const wasScanning = this._state && this._state.scanning;
        this._state = state;
        this._renderHeader();
        if (this._tab === "events" && (!state.scanning || !wasScanning)) this._renderBody();
        if (!state.scanning) {
          clearInterval(this._poll);
          this._poll = null;
          this._renderBody();
        }
      } catch (_) {
        /* keep polling; HA may be restarting */
      }
    }, POLL_MS);
  }

  // ----------------------------------------------------------------- shell

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <div class="toolbar"><span id="menu-slot"></span><div class="title">Event Scout</div></div>
      <main>
        <header>
          <div class="head-text">
            <h1>Event Scout</h1>
            <p id="status" class="muted"></p>
          </div>
          <div class="head-actions">
            <span id="spend" class="chip" title="OpenRouter usage this month for this API key"></span>
            <button id="scan" class="primary"></button>
          </div>
        </header>
        <div id="banner"></div>
        <nav class="tabs">
          <button data-tab="events">Events</button>
          <button data-tab="settings">Settings</button>
        </nav>
        <section id="body"></section>
      </main>`;
    const menu = document.createElement("ha-menu-button");
    menu.hass = this._hass;
    menu.narrow = this._narrow;
    menu.style.display = this._narrow ? "" : "none";
    this.shadowRoot.getElementById("menu-slot").appendChild(menu);
    this._menu = menu;

    this.shadowRoot.getElementById("scan").addEventListener("click", () => this._scan());
    this.shadowRoot.querySelectorAll(".tabs button").forEach((b) =>
      b.addEventListener("click", () => {
        this._tab = b.dataset.tab;
        this._renderBody();
      })
    );
    const body = this.shadowRoot.getElementById("body");
    body.addEventListener("click", (e) => this._onClick(e));
    body.addEventListener("input", (e) => {
      if (e.target.closest(".settings")) this._markDirty();
    });
    body.addEventListener("change", (e) => {
      if (e.target.dataset.action === "sort") {
        this._sort = e.target.value;
        this._renderBody();
      } else if (e.target.closest(".settings")) this._markDirty();
    });
  }

  _renderHeader() {
    const root = this.shadowRoot;
    const state = this._state;
    const status = root.getElementById("status");
    const scan = root.getElementById("scan");
    const spend = root.getElementById("spend");
    const banner = root.getElementById("banner");
    if (!status) return;

    let bannerHtml = "";
    if (this._error) bannerHtml += `<p class="banner error">${esc(this._error)}</p>`;
    if (this._notice) bannerHtml += `<p class="banner info">${esc(this._notice)}</p>`;

    if (!state) {
      status.textContent = this._error ? "" : "Loading…";
      scan.textContent = "Search now";
      scan.disabled = true;
      spend.style.display = "none";
      banner.innerHTML = bannerHtml;
      return;
    }
    const last = state.last_run;
    if (state.scanning) {
      status.innerHTML = `<span class="spinner"></span> ${esc(state.progress || "Searching…")}`;
    } else if (last) {
      let text = `Last searched ${relative(last.finished || last.started)}`;
      if (last.note) text += ` · ${last.note}`;
      else if (last.kept !== undefined) text += ` · ${last.new || 0} new, ${money(last.cost)}`;
      status.textContent = text;
      if (last.error) bannerHtml += `<p class="banner ${last.status === "ok" ? "warn" : "error"}">${esc(last.error)}</p>`;
    } else {
      status.textContent = "No searches yet.";
    }
    scan.disabled = !!state.scanning;
    scan.textContent = state.scanning ? "Searching…" : "Search now";

    const sp = state.spend;
    if (sp && typeof sp.usage_monthly === "number") {
      spend.style.display = "";
      spend.textContent = typeof sp.limit === "number"
        ? `${money(sp.usage_monthly)} of ${money(sp.limit)} this month`
        : `${money(sp.usage_monthly)} this month`;
    } else {
      spend.style.display = "none";
    }
    banner.innerHTML = bannerHtml;
    root.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === this._tab));
  }

  _renderBody() {
    if (!this._state) return;
    this.shadowRoot.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === this._tab));
    const body = this.shadowRoot.getElementById("body");
    body.innerHTML = this._tab === "settings" ? this._settingsHtml() : this._eventsHtml();
  }

  // ---------------------------------------------------------------- events

  _bucketLabel(id) {
    const bucket = (this._state.config.buckets || []).find((b) => b.id === id);
    return bucket ? bucket.label : id;
  }

  _eventsHtml() {
    const state = this._state;
    const min = state.config.min_score;
    const all = state.events || [];
    const isPick = (e) => !["dislike", "dismiss"].includes(e.status) && (e.score === null || e.score === undefined || e.score >= min);
    const groups = {
      picks: all.filter(isPick),
      all: all.filter((e) => !["dislike", "dismiss"].includes(e.status)),
      liked: all.filter((e) => e.status === "like"),
      hidden: all.filter((e) => ["dislike", "dismiss"].includes(e.status)),
    };
    let list = groups[this._filter] || groups.picks;
    const score = (e) => (typeof e.score === "number" ? e.score : -1);
    list = [...list].sort((a, b) =>
      this._sort === "score" ? score(b) - score(a) || a.start.localeCompare(b.start) : a.start.localeCompare(b.start)
    );

    const chip = (key, label) =>
      `<button class="filter ${this._filter === key ? "active" : ""}" data-action="filter" data-filter="${key}">${label} <span>${groups[key].length}</span></button>`;
    const controls = `
      <div class="controls">
        <div class="filters">${chip("picks", "Top picks")}${chip("all", "All upcoming")}${chip("liked", "Liked")}${chip("hidden", "Hidden")}</div>
        <label class="sort">Sort <select data-action="sort">
          <option value="score" ${this._sort === "score" ? "selected" : ""}>Best match</option>
          <option value="date" ${this._sort === "date" ? "selected" : ""}>Soonest</option>
        </select></label>
      </div>`;

    if (!list.length) {
      const empty = !state.config.locations.length || !state.config.interests.length
        ? "Add a location and some interests in Settings, then select Search now."
        : this._filter === "picks"
          ? "No picks yet. Run a search, or lower the minimum score in Settings."
          : "Nothing here.";
      return `${controls}<p class="empty">${esc(empty)}</p>`;
    }

    if (this._sort === "date") {
      let html = controls;
      let current = "";
      let open = false;
      for (const event of list) {
        const start = parseStart(event.start);
        const key = start ? dayKey(start) : "?";
        if (key !== current) {
          if (open) html += "</div>";
          html += `<h2 class="day">${esc(start ? dayLabel(start) : "Date unknown")}</h2><div class="events">`;
          current = key;
          open = true;
        }
        html += this._card(event, min);
      }
      return html + (open ? "</div>" : "");
    }
    return `${controls}<div class="events">${list.map((e) => this._card(e, min)).join("")}</div>`;
  }

  _card(e, min) {
    const hasScore = typeof e.score === "number";
    const tier = !hasScore ? "none" : e.score >= 8 ? "high" : e.score >= min ? "mid" : "low";
    const distance = typeof e.distance_km === "number"
      ? `${e.distance_source === "estimate" ? "~" : ""}${Math.round(e.distance_km)} km`
      : "";
    const where = [e.venue, e.town, distance].filter(Boolean).map(esc).join(" · ");
    const hidden = ["dislike", "dismiss"].includes(e.status);
    const action = (verdict, icon, title, active) =>
      `<button class="icon ${active ? "on" : ""}" title="${title}" data-action="feedback" data-id="${esc(e.id)}" data-verdict="${active ? "clear" : verdict}">
        <ha-icon icon="${icon}"></ha-icon></button>`;
    return `
      <article class="${hidden ? "faded" : ""}">
        <div class="score ${tier}" title="Match score">${hasScore ? e.score : "?"}</div>
        <div class="content">
          <h3><a href="${esc(e.url)}" target="_blank" rel="noreferrer noopener">${esc(e.title)}</a>
            ${e.status === "new" && e.first_seen && Date.now() - new Date(e.first_seen).getTime() < 26 * 3600e3 ? '<span class="new">New</span>' : ""}</h3>
          <p class="when">${whenText(e)}</p>
          ${where ? `<p class="where">${where}</p>` : ""}
          ${e.why ? `<p class="why">${esc(e.why)}</p>` : ""}
          ${e.weather_note ? `<p class="weather"><ha-icon icon="mdi:weather-cloudy-alert"></ha-icon> ${esc(e.weather_note)}</p>` : ""}
          ${e.summary ? `<p class="summary">${esc(e.summary)}</p>` : ""}
          <div class="meta">
            <span class="tag">${esc(this._bucketLabel(e.bucket))}</span>
            ${e.interest ? `<span class="tag">${esc(e.interest)}</span>` : ""}
            ${e.outdoor ? '<span class="tag">Outdoor</span>' : ""}
            <span class="actions">
            ${action("like", e.status === "like" ? "mdi:thumb-up" : "mdi:thumb-up-outline", "Interested", e.status === "like")}
            ${action("dislike", e.status === "dislike" ? "mdi:thumb-down" : "mdi:thumb-down-outline", "Not for me (teaches future searches)", e.status === "dislike")}
            ${action("dismiss", e.status === "dismiss" ? "mdi:eye-off" : "mdi:eye-off-outline", "Hide (no effect on future searches)", e.status === "dismiss")}
            </span>
          </div>
        </div>
      </article>`;
  }

  // -------------------------------------------------------------- settings

  _settingsHtml() {
    const c = this._draft;
    const s = this._state;
    const bucketOptions = (selected) =>
      c.buckets.map((b) => `<option value="${b.id}" ${b.id === selected ? "selected" : ""}>${esc(b.label)} (${b.km} km)</option>`).join("");
    const location = (l, i) => `
      <div class="row loc" data-index="${i}">
        <input data-f="city" placeholder="City" value="${esc(l.city)}">
        <input data-f="region" placeholder="Province / state" value="${esc(l.region)}">
        <input data-f="country" placeholder="Country" value="${esc(l.country)}">
        <button class="icon" title="Remove" data-action="remove-location" data-index="${i}"><ha-icon icon="mdi:close"></ha-icon></button>
      </div>`;
    const bucket = (b) => `
      <div class="row bucket" data-id="${b.id}">
        <input data-f="label" value="${esc(b.label)}" aria-label="Name">
        <label class="inline"><input data-f="km" type="number" min="1" max="2000" value="${b.km}"> km</label>
        <label class="inline">look <input data-f="lookahead_days" type="number" min="1" max="180" value="${b.lookahead_days}"> days ahead</label>
        <label class="inline">every <input data-f="refresh_days" type="number" min="1" max="30" value="${b.refresh_days}"> days</label>
        <span class="muted small">${(s.next_due || {})[b.id] ? `next search ${esc(s.next_due[b.id])}` : "no interests yet"}</span>
      </div>`;
    const interest = (it, i) => `
      <div class="row interest" data-index="${i}">
        <input data-f="name" value="${esc(it.name)}" placeholder="e.g. indie folk concerts">
        <select data-f="bucket">${bucketOptions(it.bucket)}</select>
        <button class="icon" title="Remove" data-action="remove-interest" data-index="${i}"><ha-icon icon="mdi:close"></ha-icon></button>
      </div>`;
    const weatherOptions = [
      `<option value="" ${!c.weather_entity ? "selected" : ""}>Automatic</option>`,
      `<option value="none" ${c.weather_entity === "none" ? "selected" : ""}>Don't use weather</option>`,
      ...(s.weather_entities || []).map((id) => `<option value="${esc(id)}" ${c.weather_entity === id ? "selected" : ""}>${esc(this._hass.states[id]?.attributes.friendly_name || id)}</option>`),
    ].join("");
    const engines = ["auto", "exa", "parallel", "native"]
      .map((e) => `<option value="${e}" ${c.search_engine === e ? "selected" : ""}>${e === "auto" ? "Automatic" : e[0].toUpperCase() + e.slice(1)}</option>`)
      .join("");

    return `
      <div class="settings">
        <div class="card">
          <h2>Locations</h2>
          <p class="muted">Distances are measured from these places. The first one falls back to your Home Assistant home if it can't be found on the map.</p>
          <div class="locations">${c.locations.map(location).join("")}</div>
          <button class="secondary" data-action="add-location">Add location</button>
        </div>

        <div class="card">
          <h2>Distance groups</h2>
          <p class="muted">Each interest belongs to one group. Groups searched less often save money; far-away groups look further ahead so you hear about big events early.</p>
          <div class="buckets">${c.buckets.map(bucket).join("")}</div>
        </div>

        <div class="card">
          <h2>Interests</h2>
          <p class="muted">Specific beats broad: "bluegrass jam nights" works better than "music".</p>
          <div class="interests">${c.interests.map(interest).join("") || '<p class="muted">No interests yet.</p>'}</div>
          <button class="secondary" data-action="add-interest">Add interest</button>
          <details>
            <summary>Add several at once</summary>
            <textarea id="bulk" rows="3" placeholder="One per line or comma-separated"></textarea>
            <div class="row"><select id="bulk-bucket">${bucketOptions("local")}</select>
              <button class="secondary" data-action="bulk-add">Add all</button></div>
          </details>
        </div>

        <div class="card">
          <h2>What to avoid</h2>
          <textarea id="dislikes" rows="2" placeholder="e.g. kids events, networking mixers">${esc((c.dislikes || []).join(", "))}</textarea>
        </div>

        <div class="card">
          <h2>Search options</h2>
          <div class="grid">
            <label>Nightly search <input id="schedule" type="time" value="${esc(c.schedule)}"></label>
            <label>Minimum score for picks <input id="min_score" type="number" min="0" max="10" value="${c.min_score}"></label>
            <label>Max events per search <input id="max_results" type="number" min="1" max="20" value="${c.max_results}"></label>
            <label>Weather <select id="weather_entity">${weatherOptions}</select></label>
            <label>Model <input id="model" value="${esc(c.model)}"></label>
            <label>Search engine <select id="search_engine">${engines}</select></label>
          </div>
          <label class="check"><input id="check_links" type="checkbox" ${c.check_links ? "checked" : ""}> Drop events whose links are broken</label>
        </div>

        <div class="save-bar">
          <span class="muted" id="dirty">${this._dirty ? "Unsaved changes" : ""}</span>
          <button class="primary" data-action="save">Save settings</button>
        </div>
      </div>`;
  }

  _markDirty() {
    this._dirty = true;
    const flag = this.shadowRoot.getElementById("dirty");
    if (flag) flag.textContent = "Unsaved changes";
  }

  _collect() {
    const root = this.shadowRoot;
    const val = (el, f) => el.querySelector(`[data-f="${f}"]`).value;
    const c = { ...this._draft };
    c.locations = [...root.querySelectorAll(".loc")].map((row) => ({
      ...(this._draft.locations[Number(row.dataset.index)] || {}),
      city: val(row, "city").trim(),
      region: val(row, "region").trim(),
      country: val(row, "country").trim(),
    }));
    c.buckets = [...root.querySelectorAll(".bucket")].map((row) => ({
      id: row.dataset.id,
      label: val(row, "label"),
      km: Number(val(row, "km")),
      lookahead_days: Number(val(row, "lookahead_days")),
      refresh_days: Number(val(row, "refresh_days")),
    }));
    c.interests = [...root.querySelectorAll(".interest")].map((row) => ({ name: val(row, "name").trim(), bucket: val(row, "bucket") }));
    c.dislikes = root.getElementById("dislikes").value.split(/[\n,]/).map((v) => v.trim()).filter(Boolean);
    c.schedule = root.getElementById("schedule").value;
    c.min_score = Number(root.getElementById("min_score").value);
    c.max_results = Number(root.getElementById("max_results").value);
    c.weather_entity = root.getElementById("weather_entity").value;
    c.model = root.getElementById("model").value.trim();
    c.search_engine = root.getElementById("search_engine").value;
    c.check_links = root.getElementById("check_links").checked;
    return c;
  }

  // --------------------------------------------------------------- actions

  async _onClick(e) {
    const target = e.target.closest("[data-action]");
    if (!target || target.tagName === "SELECT") return;
    const action = target.dataset.action;
    if (action === "filter") {
      this._filter = target.dataset.filter;
      this._renderBody();
    } else if (action === "feedback") {
      await this._feedback(target.dataset.id, target.dataset.verdict);
    } else if (action === "save") {
      await this._save();
    } else if (["add-location", "remove-location", "add-interest", "remove-interest", "bulk-add"].includes(action)) {
      const draft = this._collect();
      const index = Number(target.dataset.index);
      if (action === "add-location") draft.locations.push({ city: "", region: "", country: draft.locations[0]?.country || "Canada" });
      if (action === "remove-location") draft.locations.splice(index, 1);
      if (action === "add-interest") draft.interests.push({ name: "", bucket: "local" });
      if (action === "remove-interest") draft.interests.splice(index, 1);
      if (action === "bulk-add") {
        const bucket = this.shadowRoot.getElementById("bulk-bucket").value;
        const names = this.shadowRoot.getElementById("bulk").value.split(/[\n,]/).map((v) => v.trim()).filter(Boolean);
        const existing = new Set(draft.interests.map((i) => i.name.toLowerCase()));
        names.filter((n) => !existing.has(n.toLowerCase())).forEach((name) => draft.interests.push({ name, bucket }));
      }
      this._draft = draft;
      this._dirty = true;
      this._renderBody();
      if (action === "add-interest") this.shadowRoot.querySelector(".interest:last-child input")?.focus();
      if (action === "add-location") this.shadowRoot.querySelector(".loc:last-child input")?.focus();
    }
  }

  async _save() {
    this._error = "";
    this._notice = "";
    try {
      const state = await this._api("POST", "config", this._collect());
      this._setState(state, true);
      this._notice = "Settings saved.";
      setTimeout(() => { this._notice = ""; this._renderHeader(); }, 3000);
    } catch (err) {
      this._error = err.message;
    }
    this._renderHeader();
  }

  async _scan() {
    this._error = "";
    this._notice = "";
    if (this._dirty) {
      this._notice = "You have unsaved settings; the search uses the saved ones.";
    }
    try {
      this._setState(await this._api("POST", "run", { force: true }));
    } catch (err) {
      this._error = err.message;
      this._renderHeader();
    }
  }

  async _feedback(id, verdict) {
    try {
      this._setState(await this._api("POST", "feedback", { id, verdict }));
    } catch (err) {
      this._error = err.message;
      this._renderHeader();
    }
  }
}

const STYLES = `
  :host { display: block; min-height: 100%; background: var(--primary-background-color); color: var(--primary-text-color);
    font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
  .toolbar { display: none; align-items: center; height: 56px; padding: 0 4px; background: var(--app-header-background-color, var(--primary-color));
    color: var(--app-header-text-color, var(--text-primary-color)); }
  .toolbar .title { font-size: 20px; margin-left: 8px; }
  :host([narrow]) .toolbar { display: flex; }
  :host([narrow]) .head-text h1 { display: none; }
  main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; box-sizing: border-box; }
  header { display: flex; justify-content: space-between; gap: 16px; align-items: center; flex-wrap: wrap; }
  h1 { margin: 0; font-size: 28px; font-weight: 500; }
  h2 { margin: 0 0 8px; font-size: 18px; font-weight: 500; }
  h2.day { margin: 24px 0 10px; font-size: 15px; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: .04em; }
  h3 { margin: 0; font-size: 16px; font-weight: 500; line-height: 1.35; }
  p { margin: 4px 0; line-height: 1.45; }
  .muted { color: var(--secondary-text-color); }
  .small { font-size: 12px; }
  .head-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .chip { padding: 6px 12px; border-radius: 16px; background: var(--secondary-background-color); font-size: 13px; color: var(--secondary-text-color); }
  button { font: inherit; cursor: pointer; border: 0; border-radius: 8px; padding: 9px 16px; }
  button.primary { background: var(--primary-color); color: var(--text-primary-color); }
  button.secondary { background: var(--secondary-background-color); color: var(--primary-text-color); margin-top: 8px; }
  button:disabled { opacity: .6; cursor: progress; }
  button.icon { background: transparent; color: var(--secondary-text-color); padding: 6px; border-radius: 50%; line-height: 0; }
  button.icon:hover { background: var(--secondary-background-color); }
  button.icon.on { color: var(--primary-color); }
  .tabs { display: flex; gap: 4px; margin: 18px 0 14px; border-bottom: 1px solid var(--divider-color); }
  .tabs button { background: none; border-radius: 0; color: var(--secondary-text-color); border-bottom: 2px solid transparent; padding: 10px 14px; }
  .tabs button.active { color: var(--primary-color); border-bottom-color: var(--primary-color); }
  .banner { padding: 10px 14px; border-radius: 8px; margin: 12px 0 0; }
  .banner.error { background: var(--error-color); color: #fff; }
  .banner.warn { background: var(--warning-color, #ffa600); color: #000; }
  .banner.info { background: var(--secondary-background-color); }
  .spinner { display: inline-block; width: 12px; height: 12px; border: 2px solid var(--secondary-text-color); border-top-color: transparent;
    border-radius: 50%; animation: spin 1s linear infinite; vertical-align: -1px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .controls { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
  .filters { display: flex; gap: 6px; flex-wrap: wrap; }
  .filter { background: var(--secondary-background-color); color: var(--primary-text-color); border-radius: 16px; padding: 6px 12px; font-size: 14px; }
  .filter span { color: var(--secondary-text-color); margin-left: 4px; }
  .filter.active { background: var(--primary-color); color: var(--text-primary-color); }
  .filter.active span { color: inherit; opacity: .8; }
  .sort { display: flex; gap: 6px; align-items: center; color: var(--secondary-text-color); font-size: 14px; }
  .events { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
  article, .card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.2)); border: var(--ha-card-border-width, 0) solid var(--ha-card-border-color, transparent); }
  article { display: flex; gap: 14px; padding: 16px; }
  article.faded { opacity: .55; }
  .score { flex: 0 0 auto; width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center; font-weight: 600;
    background: var(--secondary-background-color); color: var(--secondary-text-color); }
  .score.high { background: var(--primary-color); color: var(--text-primary-color); }
  .score.mid { border: 2px solid var(--primary-color); color: var(--primary-color); background: transparent; }
  .content { flex: 1; min-width: 0; }
  .content a { color: var(--primary-text-color); text-decoration: none; }
  .content a:hover { color: var(--primary-color); text-decoration: underline; }
  .new { font-size: 11px; background: var(--accent-color, var(--primary-color)); color: var(--text-primary-color); border-radius: 8px; padding: 1px 6px; margin-left: 6px; vertical-align: 2px; }
  .when { font-weight: 500; color: var(--primary-color); margin-top: 6px; }
  .where { color: var(--secondary-text-color); font-size: 14px; }
  .why { margin-top: 8px; }
  .weather { color: var(--warning-color, #c77700); font-size: 14px; --mdc-icon-size: 16px; }
  .summary { color: var(--secondary-text-color); font-size: 14px; }
  .meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 10px; --mdc-icon-size: 20px; }
  .tag { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: var(--secondary-background-color); color: var(--secondary-text-color); }
  .actions { margin-left: auto; display: flex; gap: 2px; }
  .empty { padding: 40px 0; text-align: center; color: var(--secondary-text-color); }
  .settings { display: grid; gap: 16px; }
  .card { padding: 18px 20px; }
  .row { display: flex; gap: 8px; align-items: center; margin: 8px 0; flex-wrap: wrap; }
  .row > input, .row > select { flex: 1 1 140px; }
  .bucket > input[data-f="label"] { flex: 1 1 120px; }
  .inline { display: flex; align-items: center; gap: 6px; color: var(--secondary-text-color); font-size: 14px; }
  .inline input { width: 72px; }
  input, select, textarea { box-sizing: border-box; font: inherit; color: var(--primary-text-color); background: var(--card-background-color);
    border: 1px solid var(--divider-color); border-radius: 6px; padding: 8px 10px; }
  textarea { width: 100%; resize: vertical; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
  .grid label { display: flex; flex-direction: column; gap: 4px; font-size: 14px; color: var(--secondary-text-color); }
  .check { display: flex; gap: 8px; align-items: center; margin-top: 14px; }
  details { margin-top: 12px; }
  summary { cursor: pointer; color: var(--primary-color); }
  details textarea { margin-top: 8px; }
  .save-bar { position: sticky; bottom: 0; display: flex; justify-content: flex-end; align-items: center; gap: 14px; padding: 12px 0;
    background: linear-gradient(transparent, var(--primary-background-color) 30%); }
  @media (max-width: 600px) {
    main { padding: 16px 12px 40px; }
    .head-text h1 { display: none; }
    .events { grid-template-columns: 1fr; }
  }
`;

customElements.define("local-event-scout-panel", LocalEventScoutPanel);
