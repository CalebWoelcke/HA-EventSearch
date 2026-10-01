class LocalEventScoutPanel extends HTMLElement {
  set hass(value) {
    this._hass = value;
    if (!this._loaded) this._load();
  }

  async _load() {
    if (!this._hass) return;
    this._loaded = true;
    try {
      const state = await this._hass.callApi("GET", "/api/local_event_scout/config");
      this._state = state;
      this._render();
    } catch (err) {
      this._error = `Could not load Event Scout: ${err.message || err}`;
      this._render();
    }
  }

  _escape(value = "") {
    return String(value).replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#039;",'"':"&quot;"}[char]));
  }

  _errorText(error) {
    if (typeof error === "string") return error;
    if (error?.message) return error.message;
    try { return JSON.stringify(error); } catch (_) { return String(error); }
  }

  _render() {
    const state = this._state;
    if (!state) {
      this.innerHTML = `<style>${this._css()}</style><main><h1>Event Scout</h1><p>${this._escape(this._error || "Loading…")}</p></main>`;
      return;
    }
    const c = state.config;
    const items = (state.results.items || []).filter(item => !item.dismissed);
    this.innerHTML = `
      <style>${this._css()}</style>
      <main>
        <header><div><h1>Event Scout</h1><p>Local events worth your time, found overnight.</p></div>
          <button id="scan" ${state.scanning ? "disabled" : ""}>${state.scanning ? "Searching…" : "Search now"}</button></header>
        ${this._error ? `<p class="error">${this._escape(this._error)}</p>` : ""}
        <section class="settings">
          <h2>Locations</h2><div id="locations">${(c.locations || []).map(location => this._locationRow(location)).join("")}</div>
          <button class="secondary" id="add-location">Add location</button>
          <h2>What you like</h2><label>Interests <span>One per line or comma-separated</span>
            <textarea id="interests" rows="3">${this._escape((c.interests || []).join(", "))}</textarea></label>
          <h2>What to avoid</h2><label>Dislikes <span>One per line or comma-separated</span>
            <textarea id="dislikes" rows="2">${this._escape((c.dislikes || []).join(", "))}</textarea></label>
          <div class="options"><label>Nightly scan <input id="schedule" type="time" value="${this._escape(c.schedule)}"></label>
            <label>Model <input id="model" value="${this._escape(c.model)}"></label>
            <label>Search engine <select id="engine"><option value="parallel" ${c.search_engine === "parallel" ? "selected" : ""}>Parallel</option><option value="exa" ${c.search_engine === "exa" ? "selected" : ""}>Exa</option></select></label>
            <label>Max results/location <input id="max-results" type="number" min="1" max="20" value="${c.max_results}"></label></div>
          <button id="save">Save preferences</button>
        </section>
        <section><h2>Events</h2><p class="muted">${state.results.last_run ? `Last searched: ${new Date(state.results.last_run).toLocaleString()}` : "No searches have run yet."}</p>
          <div class="events">${items.length ? items.map(item => this._eventCard(item)).join("") : "<p>No events yet. Add a location and interests, then select Search now.</p>"}</div>
        </section>
      </main>`;
    this.querySelector("#scan")?.addEventListener("click", () => this._scan());
    this.querySelector("#save")?.addEventListener("click", () => this._save());
    this.querySelector("#add-location")?.addEventListener("click", () => this._addLocation());
    this.querySelectorAll(".remove-location").forEach(button => button.addEventListener("click", event => event.target.closest(".location").remove()));
  }

  _locationRow(location = {}) {
    return `<div class="location">
      <input class="city" placeholder="City" value="${this._escape(location.city || "")}">
      <input class="region" placeholder="Province/state" value="${this._escape(location.region || "")}">
      <input class="country" placeholder="Country" value="${this._escape(location.country || "Canada")}">
      <input class="radius" type="number" min="1" max="500" value="${location.radius_km || 25}" aria-label="Radius in kilometres"><span>km</span>
      <button class="icon remove-location" title="Remove location">×</button></div>`;
  }

  _eventCard(item) {
    return `<article><div><h3><a href="${this._escape(item.url)}" target="_blank" rel="noreferrer">${this._escape(item.title)}</a></h3>
      <p class="date">${this._escape(item.start)} · ${this._escape(item.venue || item.location)}</p>
      <p>${this._escape(item.summary)}</p>${item.why_interesting ? `<p class="match"><b>Why it matches:</b> ${this._escape(item.why_interesting)}</p>` : ""}
      ${item.source ? `<p class="source">Source: ${this._escape(item.source)}</p>` : ""}</div></article>`;
  }

  _locations() {
    return [...this.querySelectorAll(".location")].map(row => ({
      city: row.querySelector(".city").value,
      region: row.querySelector(".region").value,
      country: row.querySelector(".country").value,
      radius_km: Number(row.querySelector(".radius").value || 25),
    }));
  }

  _tags(id) {
    return this.querySelector(id).value.split(/[\n,]/).map(value => value.trim()).filter(Boolean);
  }

  async _save() {
    this._error = "";
    try {
      this._state = await this._hass.callApi("POST", "/api/local_event_scout/config", {
        locations: this._locations(), interests: this._tags("#interests"), dislikes: this._tags("#dislikes"),
        schedule: this.querySelector("#schedule").value, model: this.querySelector("#model").value,
        search_engine: this.querySelector("#engine").value, max_results: Number(this.querySelector("#max-results").value),
      });
    } catch (err) { this._error = this._errorText(err); }
    this._render();
  }

  async _scan() {
    this._error = "";
    this._state.scanning = true; this._render();
    try { this._state = await this._hass.callApi("POST", "/api/local_event_scout/run", {}); }
    catch (err) { this._error = this._errorText(err); this._state.scanning = false; }
    this._render();
  }

  _addLocation() {
    this.querySelector("#locations").insertAdjacentHTML("beforeend", this._locationRow());
    this.querySelector("#locations").lastElementChild.querySelector(".remove-location").addEventListener("click", event => event.target.closest(".location").remove());
  }

  _css() { return `
    :host { display:block; height:100%; overflow:auto; background:var(--primary-background-color); color:var(--primary-text-color); }
    main { max-width:1100px; margin:0 auto; padding:28px; } header { display:flex; justify-content:space-between; gap:20px; align-items:center; } h1 { margin:0; } h2 { margin:26px 0 10px; } h3 { margin:0; } p { line-height:1.45; } button { background:var(--primary-color); color:var(--text-primary-color); border:0; border-radius:8px; padding:10px 16px; font:inherit; cursor:pointer; } button:disabled { opacity:.6; cursor:wait; } button.secondary { background:var(--secondary-background-color); color:var(--primary-text-color); margin-top:8px; } button.icon { background:transparent; color:var(--secondary-text-color); font-size:24px; padding:0 8px; } .settings, article { background:var(--card-background-color); border-radius:12px; padding:20px; box-shadow:var(--ha-card-box-shadow, 0 1px 3px #0002); } label { display:block; font-weight:600; margin:10px 0; } label span, .muted, .source { display:block; color:var(--secondary-text-color); font-size:.9em; font-weight:normal; } input, textarea, select { box-sizing:border-box; width:100%; margin-top:5px; border:1px solid var(--divider-color); border-radius:6px; padding:9px; color:var(--primary-text-color); background:var(--card-background-color); font:inherit; } .location { display:grid; grid-template-columns:2fr 1.5fr 1.5fr 80px 20px 36px; gap:8px; align-items:end; margin:8px 0; } .location input { margin:0; } .location span { padding:9px 0; color:var(--secondary-text-color); } .options { display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; } .events { display:grid; grid-template-columns:repeat(auto-fill, minmax(290px, 1fr)); gap:16px; } article { padding:16px; } article a { color:var(--primary-color); text-decoration:none; } .date { font-weight:600; margin:.5em 0; } .match { border-left:3px solid var(--primary-color); padding-left:9px; } .error { padding:12px; border-radius:8px; background:var(--error-color); color:var(--text-primary-color); } @media (max-width:700px) { main { padding:16px; } header { align-items:flex-start; flex-direction:column; } .location, .options { grid-template-columns:1fr 1fr; } .location span { display:none; } }
  `; }
}
customElements.define("local-event-scout-panel", LocalEventScoutPanel);
