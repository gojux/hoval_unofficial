/*
 * Hoval system diagram card (custom:hoval-schema-card).
 *
 * Shows the components the integration found (heat pump, buffer, hot water,
 * heating circuits) as a symbolic diagram with their values. Which
 * components exist and which entity shows which value comes from the
 * integration's WebSocket command "hoval_unofficial/schema"; the values are
 * read from the entities' states. Clicking a value opens its more-info
 * dialog.
 *
 * Plain web component without a build step, served by the integration.
 */

const CARD_TYPE = "hoval-schema-card";
// Starts within the last 24 hours, read from the long-term statistics.
const STARTS_REFRESH_MS = 10 * 60 * 1000;

const TEXT = {
  en: {
    heat_pump: "Heat pump",
    buffer: "Buffer",
    heating_buffer: "Heating buffer",
    cooling_buffer: "Cooling buffer",
    hot_water: "Hot water",
    circuit: "Heating circuit {n}",
    actual_temp: "Actual temp.",
    target_temp: "Target temp.",
    return: "Return",
    heat_output: "Heat output",
    cooling_output: "Cooling output",
    modulation: "Modulation",
    flow: "Flow",
    room: "Room",
    top: "top",
    bottom: "bottom",
    middle: "middle",
    target: "Target",
    outdoor: "Outdoor temperature",
    power_input: "Power input",
    cop: "COP",
    starts: "Starts 24 h",
    gives_heat: "Gives off heat",
    not_found: "No loaded Hoval integration found.",
  },
  de: {
    heat_pump: "Wärmepumpe",
    buffer: "Puffer",
    heating_buffer: "Heizpuffer",
    cooling_buffer: "Kühlpuffer",
    hot_water: "Warmwasser",
    circuit: "Heizkreis {n}",
    actual_temp: "Ist-Temperatur",
    target_temp: "Soll-Temperatur",
    return: "Rücklauf",
    heat_output: "Wärmeleistung",
    cooling_output: "Kühlleistung",
    modulation: "Modulation",
    flow: "Vorlauf",
    room: "Raum",
    top: "oben",
    bottom: "unten",
    middle: "Mitte",
    target: "Soll",
    outdoor: "Außentemperatur",
    power_input: "Leistungsaufnahme",
    cop: "COP",
    starts: "Starts 24 h",
    gives_heat: "Gibt Wärme ab",
    not_found: "Keine geladene Hoval-Integration gefunden.",
  },
};

// Heat pump status (enum sensor state) -> what the diagram shows.
const MODE_BY_STATUS = {
  heating_mode: "heat",
  active_cooling: "cool",
  passive_cooling: "cool",
  hot_water: "dhw",
  defrost: "defrost",
};
const SMART_GRID_ACTIVE = ["preferred_operation", "forced_consumption"];
const UNUSABLE = ["unavailable", "unknown", "", undefined, null];

// Geometry of the diagram (SVG user units).
const HP_RIGHT = 200;
const DIVERTER_X = 255;
const RETURN_JOIN_X = 285;
const FLOW_MANIFOLD_X = 545;
const RETURN_MANIFOLD_X = 520;
const CIRCUIT_X = 600;
const CIRCUIT_WIDTH = 165;
const CIRCUIT_HEIGHT = 150;
const CIRCUIT_PITCH = 175;
const FLOW_Y = 85;
const RETURN_Y = 160;

const esc = (value) =>
  String(value).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

class HovalSchemaCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._schema = null;
    this._error = null;
    this._loading = false;
    this._starts = null;
    this._startsTimer = null;
    this._renderKey = null;
    this.shadowRoot.addEventListener("click", (ev) => this._onActivate(ev));
    this.shadowRoot.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") this._onActivate(ev);
    });
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { ...config };
    this._schema = null;
    this._renderKey = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._schema && !this._loading && !this._error) this._load();
    this._render();
  }

  connectedCallback() {
    if (this._schema && this._schema.heat_pump.starts) this._scheduleStarts();
  }

  disconnectedCallback() {
    clearInterval(this._startsTimer);
    this._startsTimer = null;
  }

  getCardSize() {
    return 7;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  async _load() {
    this._loading = true;
    try {
      const msg = { type: "hoval_unofficial/schema" };
      if (this._config && this._config.entry_id) msg.entry_id = this._config.entry_id;
      this._schema = await this._hass.callWS(msg);
      if (this._schema.heat_pump.starts) this._scheduleStarts();
    } catch (err) {
      this._error = err;
    }
    this._loading = false;
    this._renderKey = null;
    this._render();
  }

  _scheduleStarts() {
    if (this._startsTimer) return;
    this._loadStarts();
    this._startsTimer = setInterval(() => this._loadStarts(), STARTS_REFRESH_MS);
  }

  async _loadStarts() {
    try {
      const result = await this._hass.callWS({
        type: "recorder/statistic_during_period",
        statistic_id: this._schema.heat_pump.starts,
        rolling_window: { duration: { hours: 24 } },
        types: ["change"],
      });
      this._starts = typeof result.change === "number" ? Math.round(result.change) : null;
    } catch (err) {
      this._starts = null;
    }
    this._renderKey = null;
    this._render();
  }

  // ---- helpers -----------------------------------------------------------

  _t(key, vars = {}) {
    const lang = (this._hass && this._hass.locale && this._hass.locale.language) || "en";
    const table = TEXT[lang.split("-")[0]] || TEXT.en;
    return (table[key] || TEXT.en[key] || key).replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
  }

  _state(entityId) {
    return entityId ? this._hass.states[entityId] : undefined;
  }

  _usable(entityId) {
    const st = this._state(entityId);
    return st && !UNUSABLE.includes(st.state) ? st : undefined;
  }

  _num(value, digits = 1) {
    const n = Number(value);
    if (value === null || value === undefined || value === "" || Number.isNaN(n)) return "—";
    const lang = (this._hass.locale && this._hass.locale.language) || "en";
    return n.toLocaleString(lang, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  // A sensor's value with its unit, e.g. "40,5 °C".
  _value(entityId, digits = 1) {
    const st = this._usable(entityId);
    if (!st) return "—";
    const unit = st.attributes.unit_of_measurement;
    const text = this._num(st.state, digits);
    if (text === "—" || !unit) return text;
    return unit === "%" ? `${text} %` : `${text} ${unit}`;
  }

  _temp(value) {
    const text = this._num(value);
    return text === "—" ? text : `${text} °C`;
  }

  _formatState(entityId) {
    const st = this._usable(entityId);
    if (!st) return undefined;
    return this._hass.formatEntityState ? this._hass.formatEntityState(st) : st.state;
  }

  _onActivate(ev) {
    const target = ev.composedPath().find((el) => el.dataset && el.dataset.entity);
    if (!target) return;
    ev.preventDefault();
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        detail: { entityId: target.dataset.entity },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // ---- SVG building blocks ----------------------------------------------

  _row(x1, x2, y, label, value, target, entityId) {
    const text =
      `<text class="label" x="${x1}" y="${y}">${esc(label)}</text>` +
      `<text class="value" x="${x2}" y="${y}" text-anchor="end">${esc(value)}` +
      (target !== undefined ? `<tspan class="soft"> / ${esc(target)}</tspan>` : "") +
      `</text>`;
    return this._spot(x1 - 5, y - 14, x2 - x1 + 10, 20, entityId, text);
  }

  _spot(x, y, w, h, entityId, inner) {
    if (!entityId) return inner;
    return (
      `<g class="spot" tabindex="0" role="button" data-entity="${esc(entityId)}">` +
      `<rect class="hit" x="${x}" y="${y}" width="${w}" height="${h}" rx="5"/>${inner}</g>`
    );
  }

  _pill(x, y, text, color, maxWidth = 150, anchor = "start") {
    let label = String(text);
    const charWidth = color === "sg" ? 6.1 : 6.6;
    const pad = color === "sg" ? 12 : 18;
    const maxChars = Math.floor((maxWidth - pad) / charWidth);
    if (label.length > maxChars) label = `${label.slice(0, maxChars - 1)}…`;
    const w = Math.round(label.length * charWidth + pad);
    const h = color === "sg" ? 18 : 20;
    const left = anchor === "middle" ? x - w / 2 : x;
    return (
      `<rect class="pill-bg ${color}" x="${left}" y="${y}" width="${w}" height="${h}" rx="${h / 2}"/>` +
      `<text class="pill ${color}" x="${left + w / 2}" y="${y + (color === "sg" ? 12.5 : 14)}" text-anchor="middle">${esc(label)}</text>`
    );
  }

  // Smart Grid offset badge: only while Smart Grid is in preferred operation
  // or forced consumption, and only for an offset other than 0.
  _sgBadge(x, y, offsetEntity, anchor = "start") {
    if (!this._sgActive) return "";
    const st = this._usable(offsetEntity);
    const offset = st ? Number(st.state) : 0;
    if (!offset) return "";
    const sign = offset > 0 ? "+" : "−";
    const text = `SG ${sign}${this._num(Math.abs(offset))} K`;
    return this._spot(anchor === "middle" ? x - 36 : x - 2, y - 2, 72, 22, offsetEntity, this._pill(x, y, text, "sg", 80, anchor));
  }

  _tri(points, color) {
    return `<polygon class="sym f-${color}" points="${points}"/>`;
  }

  _pump(cx, cy, color) {
    return (
      `<circle class="pump c-${color}" cx="${cx}" cy="${cy}" r="9"/>` +
      `<polygon class="f-${color}" points="${cx - 4},${cy - 5} ${cx - 4},${cy + 5} ${cx + 5},${cy}"/>`
    );
  }

  // ---- rendering ---------------------------------------------------------

  _relevantEntities() {
    const s = this._schema;
    const ids = [...Object.values(s.heat_pump), s.smart_grid_status];
    for (const b of s.buffers) ids.push(b.top, b.bottom, b.middle, b.sg_offset_heating, b.sg_offset_cooling);
    if (s.hot_water) ids.push(s.hot_water.water_heater, s.hot_water.bottom, s.hot_water.sg_offset);
    for (const c of s.circuits) ids.push(c.climate, c.flow, c.flow_target, c.status, c.sg_offset_heating, c.sg_offset_cooling);
    return ids.filter(Boolean);
  }

  _render() {
    if (!this._hass) return;
    if (this._error) {
      this._renderShell(`<div class="message">${esc(this._t("not_found"))}</div>`);
      return;
    }
    if (!this._schema) return;
    // Only redraw when a shown state changed, so the flow animation keeps running.
    const key = JSON.stringify([
      this._hass.locale && this._hass.locale.language,
      this._starts,
      this._relevantEntities().map((id) => {
        const st = this._hass.states[id];
        return st ? [st.state, st.attributes.current_temperature, st.attributes.temperature, st.attributes.hvac_action] : null;
      }),
    ]);
    if (key === this._renderKey) return;
    this._renderKey = key;
    this._renderShell(this._diagram());
  }

  _renderShell(body) {
    const s = this._schema;
    const title = (this._config && this._config.title) || (s && s.title) || "Hoval";
    const sgChip =
      s && this._sgActive
        ? `<span class="chip spot" tabindex="0" role="button" data-entity="${esc(s.smart_grid_status)}">Smart Grid · ${esc(this._formatState(s.smart_grid_status) || "")}</span>`
        : "";
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="head"><div class="title">${esc(title)}</div>${sgChip}</div>
        ${body}
      </ha-card>`;
  }

  _diagram() {
    const s = this._schema;
    const hp = s.heat_pump;
    const statusState = this._usable(hp.status);
    const mode = (statusState && MODE_BY_STATUS[statusState.state]) || "idle";
    const sgState = this._usable(s.smart_grid_status);
    this._sgActive = !!(sgState && SMART_GRID_ACTIVE.includes(sgState.state));

    const defrost = mode === "defrost";
    const hpColor = { heat: "hot", cool: "cold", dhw: "hot", defrost: "amber", idle: "idle" }[mode];
    const hasBuffer = s.buffers.length > 0;
    const hasDhw = !!s.hot_water;
    const bufferFeed = ["heat", "cool", "defrost"].includes(mode);
    const dhwFeed = mode === "dhw";

    // Heating circuits: running and their colour.
    const circuits = s.circuits.map((c, i) => {
      const climate = this._usable(c.climate);
      const action = climate && climate.attributes.hvac_action;
      // Without a buffer a circuit only gets water while the heat pump
      // heats or cools for it, or takes defrost heat from it.
      const supplied = hasBuffer || bufferFeed;
      const on = supplied && (action === "heating" || action === "cooling");
      const color = !on ? "idle" : defrost ? "amber" : action === "cooling" ? "cold" : "hot";
      const top = 40 + i * CIRCUIT_PITCH;
      return { ...c, climate, action, on, color, top, flowY: top + 45, returnY: top + 120 };
    });
    const anyOn = circuits.some((c) => c.on);
    const lineColor = circuits.find((c) => c.on)?.color || "idle";
    const last = circuits[circuits.length - 1];

    const pipes = [];
    const add = (d, role, on, color) => pipes.push({ d, role, on, color: on ? color : "idle" });
    add(`M${HP_RIGHT} ${FLOW_Y} H${DIVERTER_X}`, "f", mode !== "idle", hpColor);
    add(`M${RETURN_JOIN_X} ${RETURN_Y} H${HP_RIGHT}`, "r", mode !== "idle", hpColor);
    if (hasDhw) {
      add(`M${DIVERTER_X} ${FLOW_Y} V330 H${HP_RIGHT}`, "f", dhwFeed, "hot");
      add(`M${HP_RIGHT} 385 H${RETURN_JOIN_X} V${RETURN_Y}`, "r", dhwFeed, "hot");
    }

    let bufferSvg = "";
    if (s.buffers.length === 1) {
      const b = s.buffers[0];
      add(`M${DIVERTER_X} ${FLOW_Y} H360`, "f", bufferFeed, hpColor);
      add(`M360 ${RETURN_Y} H${RETURN_JOIN_X}`, "r", bufferFeed, hpColor);
      add(`M456 ${FLOW_Y} H${FLOW_MANIFOLD_X}`, "f", anyOn, lineColor);
      add(`M${RETURN_MANIFOLD_X} ${RETURN_Y} H456`, "r", anyOn, lineColor);
      bufferSvg = this._singleBuffer(b, mode, defrost);
    } else if (s.buffers.length === 2) {
      const heating = s.buffers.find((b) => b.role === "heating_buffer") || s.buffers[0];
      const cooling = s.buffers.find((b) => b.role === "cooling_buffer") || s.buffers[1];
      const cooling_on = mode === "cool" || circuits.some((c) => c.on && c.action === "cooling");
      const heating_on = !cooling_on && (["heat", "defrost"].includes(mode) || circuits.some((c) => c.on));
      const heatColor = defrost ? "amber" : "hot";
      const on = bufferFeed || anyOn;
      const color = cooling_on ? "cold" : heatColor;
      add(`M${DIVERTER_X} ${FLOW_Y} H${FLOW_MANIFOLD_X}`, "f", on, color);
      add(`M${RETURN_MANIFOLD_X} ${RETURN_Y} V250 H${RETURN_JOIN_X} V${RETURN_Y}`, "r", on, color);
      add(`M365 ${FLOW_Y} V100`, "f", heating_on, heatColor);
      add("M365 230 V250", "r", heating_on, heatColor);
      add(`M455 ${FLOW_Y} V100`, "f", cooling_on, "cold");
      add("M455 230 V250", "r", cooling_on, "cold");
      bufferSvg = this._splitBuffers(heating, cooling, heating_on, cooling_on, defrost);
    } else {
      const on = anyOn && ["heat", "cool", "defrost"].includes(mode);
      add(`M${DIVERTER_X} ${FLOW_Y} H${FLOW_MANIFOLD_X}`, "f", on, lineColor);
      add(`M${RETURN_MANIFOLD_X} ${RETURN_Y} H${RETURN_JOIN_X}`, "r", on, lineColor);
    }

    for (const c of circuits) {
      if (c.flowY === FLOW_Y) {
        add(`M${FLOW_MANIFOLD_X} ${FLOW_Y} H${CIRCUIT_X}`, "f", c.on, c.color);
        add(`M${CIRCUIT_X} ${RETURN_Y} H${RETURN_MANIFOLD_X}`, "r", c.on, c.color);
      } else {
        add(`M${FLOW_MANIFOLD_X} ${FLOW_Y} V${c.flowY} H${CIRCUIT_X}`, "f", c.on, c.color);
        add(`M${CIRCUIT_X} ${c.returnY} H${RETURN_MANIFOLD_X} V${RETURN_Y}`, "r", c.on, c.color);
      }
      // Mixing valve bypass from the return.
      add(`M565 ${c.returnY} V${c.flowY}`, "r", c.on, c.color);
    }

    // Returns first, so a flow line crossing a return cuts it visibly.
    const ordered = [...pipes.filter((p) => p.role === "r"), ...pipes.filter((p) => p.role === "f")];
    let pipeSvg = "";
    for (const p of ordered) {
      pipeSvg += `<path class="halo" d="${p.d}"/><path class="pipe ${p.role} c-${p.color}" d="${p.d}"/>`;
      if (p.on) pipeSvg += `<path class="flow" d="${p.d}"/>`;
    }

    let symbols = "";
    if (hasDhw) {
      const toBuffer = bufferFeed || (!hasBuffer && anyOn);
      symbols +=
        this._tri(`${DIVERTER_X - 12},78 ${DIVERTER_X - 12},92 ${DIVERTER_X},85`, mode !== "idle" ? hpColor : "idle") +
        this._tri(`${DIVERTER_X + 12},78 ${DIVERTER_X + 12},92 ${DIVERTER_X},85`, toBuffer ? hpColor : "idle") +
        this._tri(`${DIVERTER_X - 7},97 ${DIVERTER_X + 7},97 ${DIVERTER_X},85`, dhwFeed ? "hot" : "idle");
    }
    // Heat pump circulation pump (CP), in the flow inside the indoor unit.
    symbols += this._pump(229, FLOW_Y, mode !== "idle" ? hpColor : "idle");
    for (const c of circuits) {
      const y = c.flowY;
      symbols +=
        this._tri(`553,${y - 7} 553,${y + 7} 565,${y}`, c.color) +
        this._tri(`577,${y - 7} 577,${y + 7} 565,${y}`, c.color) +
        this._tri(`558,${y + 12} 572,${y + 12} 565,${y}`, c.color) +
        this._pump(588, y, c.color);
    }

    const height = Math.max(hasDhw ? 465 : 250, last ? last.top + CIRCUIT_HEIGHT + 15 : 0);
    return `
      <div class="diagram">
        <svg viewBox="0 0 780 ${height}" role="img" aria-label="${esc(this._t("heat_pump"))}">
          ${bufferSvg}${pipeSvg}${symbols}
          ${this._heatPump(hp, mode, hpColor, statusState)}
          ${hasDhw ? this._hotWater(s.hot_water, dhwFeed) : ""}
          ${circuits.map((c) => this._circuit(c, defrost)).join("")}
          <text class="small" x="204" y="72">VL</text><text class="small" x="204" y="176">RL</text>
        </svg>
      </div>
      ${this._footer(hp)}`;
  }

  _heatPump(hp, mode, color, statusState) {
    const cooling = mode === "cool";
    const power = cooling ? hp.cooling_power : hp.power;
    const status = statusState ? this._formatState(hp.status) : "—";
    const rows = [
      [this._t("actual_temp"), hp.temperature, 1],
      [this._t("target_temp"), hp.target, 1],
      [this._t("return"), hp.return, 1],
      [this._t(cooling ? "cooling_output" : "heat_output"), power, 1],
      [this._t("modulation"), hp.modulation, 0],
    ].filter(([, id]) => id);
    let svg =
      `<rect class="box" x="20" y="40" width="180" height="170" rx="12"/>` +
      `<text class="title" x="34" y="64">${esc(this._t("heat_pump"))}</text>` +
      this._spot(30, 72, 160, 26, hp.status, this._pill(34, 74, status, mode === "idle" ? "idle" : color, 152));
    rows.forEach(([label, id, digits], i) => {
      svg += this._row(34, 186, 117 + i * 20, label, this._value(id, digits), undefined, id);
    });
    return svg;
  }

  _singleBuffer(b, mode, defrost) {
    const tint = mode === "idle" || mode === "dhw" ? "tank" : mode === "cool" ? "cold-tint" : defrost ? "amber-tint" : "hot-tint";
    const offset = mode === "cool" ? b.sg_offset_cooling : b.sg_offset_heating;
    let svg =
      `<defs><linearGradient id="hsc-buffer" x1="0" y1="0" x2="0" y2="1">` +
      `<stop offset="0" style="stop-color:var(--hsc-${tint})"/><stop offset="1" style="stop-color:var(--hsc-tank)"/></linearGradient></defs>` +
      `<rect class="tank" x="360" y="40" width="96" height="190" rx="24" fill="url(#hsc-buffer)"/>`;
    if (b.middle) {
      svg +=
        `<text class="title" x="408" y="120" text-anchor="middle">${esc(this._t("buffer"))}</text>` +
        this._spot(368, 128, 80, 24, b.middle, `<text class="value" x="408" y="145" text-anchor="middle">${esc(this._value(b.middle))}</text>`) +
        this._sgBadge(408, 160, offset, "middle");
      return svg;
    }
    svg += `<text class="title" x="408" y="134" text-anchor="middle">${esc(this._t("buffer"))}</text>`;
    svg += this._sgBadge(408, 144, offset, "middle");
    if (b.top) {
      svg += this._spot(368, 56, 80, 42, b.top,
        `<text class="small" x="408" y="72" text-anchor="middle">${esc(this._t("top"))}</text>` +
        `<text class="value" x="408" y="90" text-anchor="middle">${esc(this._value(b.top))}</text>`);
    }
    if (b.bottom) {
      svg += this._spot(368, 176, 80, 42, b.bottom,
        `<text class="small" x="408" y="192" text-anchor="middle">${esc(this._t("bottom"))}</text>` +
        `<text class="value" x="408" y="210" text-anchor="middle">${esc(this._value(b.bottom))}</text>`);
    }
    return svg;
  }

  _splitBuffers(heating, cooling, heatingOn, coolingOn, defrost) {
    const tank = (x, b, on, tint, nameKey, prefer, offset, badgeY) => {
      const id = b[prefer] || b[prefer === "top" ? "bottom" : "top"];
      const where = b[prefer] ? prefer : prefer === "top" ? "bottom" : "top";
      const valueY = where === "top" ? 150 : 194;
      return (
        `<rect class="tank" x="${x - 35}" y="100" width="70" height="130" rx="18" style="fill:var(--hsc-${on ? tint : "tank"})"/>` +
        `<text class="title sm" x="${x}" y="124" text-anchor="middle">${esc(this._t(nameKey))}</text>` +
        (id
          ? this._spot(x - 31, valueY - 14, 62, 40, id,
              `<text class="small" x="${x}" y="${valueY}" text-anchor="middle">${esc(this._t(where))}</text>` +
              `<text class="value" x="${x}" y="${valueY + 18}" text-anchor="middle">${esc(this._value(id))}</text>`)
          : "") +
        this._sgBadge(x, badgeY, offset, "middle")
      );
    };
    return (
      tank(365, heating, heatingOn, defrost ? "amber-tint" : "hot-tint", "heating_buffer", "top", heating.sg_offset_heating, 190) +
      tank(455, cooling, coolingOn, "cold-tint", "cooling_buffer", "bottom", cooling.sg_offset_cooling, 136)
    );
  }

  _hotWater(dhw, loading) {
    const heater = this._usable(dhw.water_heater);
    const attrs = heater ? heater.attributes : {};
    const id = dhw.water_heater;
    let svg =
      `<rect class="tank" x="40" y="280" width="160" height="${dhw.sg_offset && this._sgActive ? 170 : 150}" rx="28" style="fill:var(--hsc-${loading ? "hot-tint" : "tank"})"/>` +
      `<path class="pipe c-${loading ? "hot" : "idle"}" style="stroke-width:3" d="M200 330 H178 L160 338 L190 346 L160 354 L190 362 L160 370 L178 378 V385 H200"/>` +
      `<text class="title" x="58" y="308">${esc(this._t("hot_water"))}</text>`;
    if (dhw.bottom) {
      svg +=
        this._row(58, 148, 340, this._t("top"), this._temp(attrs.current_temperature), undefined, id) +
        this._row(58, 148, 362, this._t("bottom"), this._value(dhw.bottom), undefined, dhw.bottom) +
        this._spot(52, 370, 100, 20, id, `<text class="label" x="58" y="384">${esc(this._t("target"))} ${esc(this._temp(attrs.temperature))}</text>`);
    } else {
      svg +=
        `<text class="small" x="58" y="328">${esc(this._t("middle"))}</text>` +
        this._spot(52, 330, 100, 34, id, `<text class="big" x="58" y="356">${esc(this._temp(attrs.current_temperature))}</text>`) +
        this._spot(52, 368, 100, 20, id, `<text class="label" x="58" y="382">${esc(this._t("target"))} ${esc(this._temp(attrs.temperature))}</text>`);
    }
    const mode = heater ? this._formatState(id) : "—";
    svg += this._spot(52, 392, 100, 26, id, this._pill(58, 396, mode, "idle", 96));
    svg += this._sgBadge(58, 420, dhw.sg_offset);
    return svg;
  }

  _circuit(c, defrost) {
    const top = c.top;
    let status;
    if (defrost && c.on) status = this._t("gives_heat");
    else if (this._usable(c.status)) status = this._formatState(c.status);
    else if (c.climate && c.action) {
      status = this._hass.formatEntityAttributeValue
        ? this._hass.formatEntityAttributeValue(c.climate, "hvac_action")
        : c.action;
    } else status = "—";
    const flowTarget = this._usable(c.flow_target);
    const room = c.climate ? c.climate.attributes : {};
    const offset = c.action === "cooling" ? c.sg_offset_cooling : c.sg_offset_heating;
    return (
      `<rect class="box" x="${CIRCUIT_X}" y="${top}" width="${CIRCUIT_WIDTH}" height="${CIRCUIT_HEIGHT}" rx="12"/>` +
      `<text class="title" x="614" y="${top + 24}">${esc(this._t("circuit", { n: c.number }))}</text>` +
      this._spot(610, top + 32, 150, 26, c.status || (c.climate && c.climate.entity_id), this._pill(614, top + 34, status, c.color, 145)) +
      this._row(614, 752, top + 82, this._t("flow"), this._value(c.flow), flowTarget ? this._num(flowTarget.state) : undefined, c.flow) +
      this._row(614, 752, top + 105, this._t("room"), this._temp(room.current_temperature),
        room.temperature !== undefined && room.temperature !== null ? this._num(room.temperature) : undefined,
        c.climate && c.climate.entity_id) +
      this._sgBadge(614, top + 120, offset)
    );
  }

  _footer(hp) {
    const stat = (label, value, entityId) =>
      entityId
        ? `<div class="stat spot" tabindex="0" role="button" data-entity="${esc(entityId)}"><span>${esc(label)}</span><b>${esc(value)}</b></div>`
        : "";
    const starts = this._starts === null ? "—" : String(this._starts);
    return `<div class="foot">
      ${stat(this._t("outdoor"), this._value(hp.outdoor), hp.outdoor)}
      ${stat(this._t("power_input"), this._value(hp.electrical_power, 2), hp.electrical_power)}
      ${stat(this._t("cop"), this._value(hp.cop), hp.cop)}
      ${stat(this._t("starts"), starts, hp.starts)}
    </div>`;
  }
}

const STYLE = `
  :host {
    --hsc-card: var(--ha-card-background, var(--card-background-color, #fff));
    --hsc-fg: var(--primary-text-color, #1d232c);
    --hsc-muted: var(--secondary-text-color, #66707e);
    --hsc-line: var(--divider-color, #d6dce4);
    --hsc-idle: var(--disabled-text-color, #bdbdbd);
    --hsc-hot: var(--state-climate-heat-color, #ff8100);
    --hsc-cold: var(--state-climate-cool-color, #2b9af9);
    --hsc-amber: var(--warning-color, #ffa600);
    --hsc-sg: var(--success-color, #43a047);
    --hsc-box: color-mix(in srgb, var(--hsc-fg) 3%, var(--hsc-card));
    --hsc-tank: color-mix(in srgb, var(--hsc-fg) 6%, var(--hsc-card));
    --hsc-hot-tint: color-mix(in srgb, var(--hsc-hot) 18%, var(--hsc-card));
    --hsc-cold-tint: color-mix(in srgb, var(--hsc-cold) 18%, var(--hsc-card));
    --hsc-amber-tint: color-mix(in srgb, var(--hsc-amber) 18%, var(--hsc-card));
    --hsc-sg-tint: color-mix(in srgb, var(--hsc-sg) 16%, var(--hsc-card));
  }
  ha-card { overflow: hidden; }
  .head { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px 2px; }
  .head .title { font-size: var(--ha-card-header-font-size, 20px); color: var(--hsc-fg); }
  .chip { font-size: 12.5px; font-weight: 500; color: var(--hsc-sg); background: var(--hsc-sg-tint); border-radius: 16px; padding: 4px 12px; white-space: nowrap; cursor: pointer; }
  .diagram { padding: 0 8px; }
  svg { display: block; width: 100%; height: auto; font-family: inherit; }
  svg text { font-variant-numeric: tabular-nums; }
  .message { padding: 16px; color: var(--hsc-muted); }
  .box { fill: var(--hsc-box); stroke: var(--hsc-line); stroke-width: 1; }
  .tank { stroke: var(--hsc-line); stroke-width: 1.5; }
  text.title { font-size: 14px; font-weight: 600; fill: var(--hsc-fg); }
  text.title.sm { font-size: 12.5px; }
  .label { font-size: 12.5px; fill: var(--hsc-muted); }
  .value { font-size: 13px; font-weight: 600; fill: var(--hsc-fg); }
  .soft { font-weight: 400; fill: var(--hsc-muted); }
  .small { font-size: 11.5px; fill: var(--hsc-muted); }
  .big { font-size: 24px; font-weight: 500; fill: var(--hsc-fg); }
  .halo { fill: none; stroke: var(--hsc-card); stroke-width: 10; }
  .pipe { fill: none; stroke-width: 4; stroke-linecap: round; stroke-linejoin: round; }
  .pipe.r { stroke-opacity: .5; }
  .c-idle { stroke: var(--hsc-idle); } .c-hot { stroke: var(--hsc-hot); } .c-cold { stroke: var(--hsc-cold); } .c-amber { stroke: var(--hsc-amber); }
  .f-idle { fill: var(--hsc-idle); } .f-hot { fill: var(--hsc-hot); } .f-cold { fill: var(--hsc-cold); } .f-amber { fill: var(--hsc-amber); }
  .flow { fill: none; stroke: var(--hsc-card); stroke-width: 1.6; stroke-dasharray: 3 11; stroke-linecap: round; opacity: .9; animation: hsc-flow .9s linear infinite; }
  @keyframes hsc-flow { to { stroke-dashoffset: -14; } }
  @media (prefers-reduced-motion: reduce) { .flow { animation: none; } }
  .sym { stroke: var(--hsc-card); stroke-width: 1.5; stroke-linejoin: round; }
  .pump { fill: var(--hsc-card); stroke-width: 2.5; }
  .pill-bg.hot { fill: var(--hsc-hot-tint); } .pill-bg.cold { fill: var(--hsc-cold-tint); } .pill-bg.amber { fill: var(--hsc-amber-tint); }
  .pill-bg.idle { fill: var(--hsc-tank); } .pill-bg.sg { fill: var(--hsc-sg-tint); }
  .pill { font-size: 11.5px; font-weight: 600; }
  .pill.hot { fill: var(--hsc-hot); } .pill.cold { fill: var(--hsc-cold); } .pill.amber { fill: var(--hsc-amber); }
  .pill.idle { fill: var(--hsc-muted); } .pill.sg { fill: var(--hsc-sg); font-size: 10.5px; }
  .hit { fill: transparent; }
  .spot { cursor: pointer; outline: none; }
  .spot:hover .value, .spot:focus-visible .value, .spot:hover .big { text-decoration: underline; }
  .spot:focus-visible .hit { stroke: var(--primary-color, #03a9f4); stroke-width: 1.5; }
  .foot { display: flex; flex-wrap: wrap; gap: 6px 26px; padding: 10px 16px 14px; border-top: 1px solid var(--hsc-line); }
  .stat { display: grid; }
  .stat span { font-size: 11.5px; color: var(--hsc-muted); }
  .stat b { font-size: 15px; font-weight: 500; color: var(--hsc-fg); font-variant-numeric: tabular-nums; }
  .stat:hover b { text-decoration: underline; }
  .stat:focus-visible, .chip:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; }
`;

if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, HovalSchemaCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD_TYPE,
    name: "Hoval system diagram",
    description: "Heat pump, buffer, hot water and heating circuits with their values.",
    preview: false,
  });
}
