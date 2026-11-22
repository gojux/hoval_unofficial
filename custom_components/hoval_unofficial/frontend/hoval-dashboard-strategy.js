/*
 * Hoval dashboard (strategy custom:hoval).
 *
 * Builds a dashboard from what the integration found: an overview with the
 * system diagram, the thermostats and the heat per day, plus views for the
 * history, the energy and the settings and diagnostics. Which entities exist
 * comes from the WebSocket command "hoval_unofficial/schema"; disabled
 * entities are left out, and every card without entities is skipped.
 *
 * The dashboard is generated each time it is opened. To change it, use
 * "Take control" in the dashboard's edit mode; from then on it is a normal
 * dashboard that no longer follows the integration.
 *
 * Plain web component without a build step, served by the integration.
 */

const STRATEGY_TYPE = "hoval";
const STRATEGY_TAG = `ll-strategy-dashboard-${STRATEGY_TYPE}`;
const STRATEGY_EDITOR_TAG = "hoval-dashboard-strategy-editor";

const TEXT = {
  en: {
    name: "Hoval heat pump",
    dashboard_title: "Heat pump",
    description: "System diagram, thermostats, history, energy and settings of the Hoval integration.",
    overview: "Overview",
    history: "History",
    energy: "Energy",
    settings: "Settings & diagnostics",
    heat_per_day: "Heat per day",
    electricity_per_day: "Electricity per day",
    starts_per_day: "Starts per day",
    counters: "Counters",
    status: "Status",
    heat_generator_buffer: "Heat generator & buffer",
    heating_circuit: "Heating circuit {number}",
    hot_water: "Hot water",
    power_modulation: "Power and modulation",
    smart_grid: "Smart Grid",
    diagnostics: "Diagnostics",
    heating: "Heating",
    actual: "Actual",
    target: "Target",
    return: "Return",
    flow: "Flow",
    flow_target: "Flow target",
    tank: "Tank",
    top: "top",
    bottom: "bottom",
    buffer: "Buffer",
    heating_buffer: "Heating buffer",
    cooling_buffer: "Cooling buffer",
    heat_output: "Heat output",
    cooling_output: "Cooling output",
    power_input: "Power input",
    modulation: "Modulation",
    cooling: "Cooling",
    room: "Room",
    heat_pump: "Heat pump",
    starts: "Starts",
    disabled_hint: "Many settings and diagnostic entities of the integration are disabled by default. Once enabled, they appear here.",
    not_found: "No loaded Hoval integration found.",
    editor_entry_id: "Integration entry",
    editor_entry_id_helper: "Only needed with more than one heat pump.",
  },
  de: {
    name: "Hoval-Wärmepumpe",
    dashboard_title: "Wärmepumpe",
    description: "Anlagenschema, Thermostate, Verlauf, Energie und Einstellungen der Hoval-Integration.",
    overview: "Übersicht",
    history: "Verlauf",
    energy: "Energie",
    settings: "Einstellungen & Diagnose",
    heat_per_day: "Wärme pro Tag",
    electricity_per_day: "Strom pro Tag",
    starts_per_day: "Starts pro Tag",
    counters: "Zählerstände",
    status: "Status",
    heat_generator_buffer: "Wärmeerzeuger & Puffer",
    heating_circuit: "Heizkreis {number}",
    hot_water: "Warmwasser",
    power_modulation: "Leistung und Modulation",
    smart_grid: "Smart Grid",
    diagnostics: "Diagnose",
    heating: "Heizen",
    actual: "Ist",
    target: "Soll",
    return: "Rücklauf",
    flow: "Vorlauf",
    flow_target: "Vorlauf-Soll",
    tank: "Speicher",
    top: "oben",
    bottom: "unten",
    buffer: "Puffer",
    heating_buffer: "Heizpuffer",
    cooling_buffer: "Kühlpuffer",
    heat_output: "Wärmeleistung",
    cooling_output: "Kälteleistung",
    power_input: "Leistungsaufnahme",
    modulation: "Modulation",
    cooling: "Kühlen",
    room: "Raum",
    heat_pump: "Wärmepumpe",
    starts: "Starts",
    disabled_hint: "Viele Einstellungs- und Diagnose-Entitäten der Integration sind standardmäßig deaktiviert. Sobald sie aktiviert sind, erscheinen sie hier.",
    not_found: "Keine geladene Hoval-Integration gefunden.",
    editor_entry_id: "Integrationseintrag",
    editor_entry_id_helper: "Nur bei mehr als einer Wärmepumpe nötig.",
  },
};

function text(lang, key, vars = {}) {
  const table = TEXT[(lang || "en").split("-")[0]] || TEXT.en;
  return (table[key] || TEXT.en[key] || key).replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");
}

// Days shown by the bar charts.
const OVERVIEW_DAYS = 7;
const ENERGY_DAYS = 30;
const HISTORY_HOURS = 24;

// Keys of the entities on the energy view's counter card, in this order.
const COUNTER_KEYS = [
  "seasonal_performance_factor",
  "heat_quantity_heat_generator",
  "heat_quantity_heating",
  "heat_quantity_hot_water",
  "cooling_energy",
  "total_electrical_energy_heat_generator",
  "operating_hours_heat_generator",
  "operating_hours_heat_generator_above_50",
  "switching_cycles_heat_generator",
  "switching_cycles_heat_generator_above_50",
];
// Values of a circuit that its settings card leaves out: the history view
// shows the temperatures, and the mixing valve goes to the diagnostics.
const CIRCUIT_SKIPPED_KEYS = ["flow_temp_hc", "supply_target_temp_hc", "mixing_valve_hc"];

class HovalDashboardStrategy extends HTMLElement {
  static async generate(config, hass) {
    const lang = hass.locale && hass.locale.language;
    const t = (key, vars) => text(lang, key, vars);
    let schema;
    try {
      schema = await hass.callWS({
        type: "hoval_unofficial/schema",
        ...(config.entry_id ? { entry_id: config.entry_id } : {}),
      });
    } catch (err) {
      return {
        views: [{
          title: t("overview"),
          cards: [{ type: "markdown", content: t("not_found") }],
        }],
      };
    }
    return { title: schema.title, views: buildViews(schema, hass, t) };
  }

  // Prefills the "Add dashboard" form.
  static getCreateSuggestions(hass) {
    return { title: text(hass.locale && hass.locale.language, "dashboard_title"), icon: "mdi:heat-pump" };
  }

  static getConfigElement() {
    return document.createElement(STRATEGY_EDITOR_TAG);
  }
}

function buildViews(schema, hass, t) {
  const entities = schema.entities || {};
  const used = new Set(schemaEntities(schema));
  // The entity of a key, if it exists and is enabled; marks it as shown.
  const e = (key) => {
    const id = entities[key];
    if (id) used.add(id);
    return id;
  };
  const list = (...keys) => keys.map(e).filter(Boolean);
  // A row for a card: the entity with its name without the device name.
  const row = (id, name) => id && { entity: id, name: name || entityName(hass, id) };
  const circuitName = (hc) => (hc.climate && entityName(hass, hc.climate))
    || t("heating_circuit", { number: hc.number });
  // A row whose name leaves out what the card's title already says.
  const shortRow = (id, patterns) => id && row(id, withoutTitle(entityName(hass, id), patterns));

  const heatRows = () => [
    row(e("heat_quantity_heating"), t("heating")),
    row(e("heat_quantity_hot_water"), t("hot_water")),
    row(e("cooling_energy"), t("cooling")),
  ];

  // Overview: the diagram on the left half; on the right the thermostats
  // side by side (up to three in a row), and the heat per day below them.
  const thermostats = [
    ...schema.circuits.map((hc) => hc.climate
      && { type: "thermostat", entity: hc.climate, name: circuitName(hc) }),
    schema.hot_water && schema.hot_water.water_heater
      && { type: "thermostat", entity: schema.hot_water.water_heater, name: t("hot_water") },
  ].filter(Boolean);
  const thermostatColumns = thermostats.length === 3 ? 8 : 12;
  const overview = view(t("overview"), "overview", "mdi:heat-pump", 4, [
    section(2, [{ type: "custom:hoval-schema-card", entry_id: schema.entry_id }]),
    section(2, [
      ...thermostats.map((card) => ({ ...card, grid_options: { columns: thermostatColumns } })),
      statistics(t("heat_per_day"), heatRows(), OVERVIEW_DAYS),
    ]),
  ]);

  // History of the last 24 hours.
  const hp = schema.heat_pump;
  // The legends name only what differs within a chart; the chart's title
  // says what it shows.
  const buffers = schema.buffers.flatMap((b) => {
    const name = t(b.role);
    return [
      row(b.top, `${name} ${t("top")}`),
      row(b.middle, name),
      row(b.bottom, `${name} ${t("bottom")}`),
    ];
  });
  const dhw = schema.hot_water;
  const history = view(t("history"), "history", "mdi:chart-line", 2, [
    section(2, [graph(t("status"), [
      row(hp.status, t("heat_pump")),
      ...schema.circuits.map((hc) => row(hc.status, circuitName(hc))),
      row(schema.smart_grid_status, t("smart_grid")),
    ])]),
    section(1, [graph(t("heat_generator_buffer"), [
      row(hp.temperature, t("actual")),
      row(hp.target, t("target")),
      row(hp.return, t("return")),
      ...buffers,
    ])]),
    // HA names a thermostat's lines "<name> current/target temperature".
    ...schema.circuits.map((hc) => section(1, [graph(circuitName(hc), [
      row(hc.flow, t("flow")), row(hc.flow_target, t("flow_target")), row(hc.climate, t("room")),
    ])])),
    dhw && section(1, [graph(t("hot_water"), [
      row(dhw.water_heater, t("tank")), row(dhw.bottom, `${t("tank")} ${t("bottom")}`),
    ])]),
    section(1, [graph(t("power_modulation"), [
      row(hp.power, t("heat_output")),
      row(hp.cooling_power, t("cooling_output")),
      row(hp.electrical_power, t("power_input")),
      row(hp.modulation, t("modulation")),
    ])]),
  ]);

  // Energy per day and the counters.
  const energy = view(t("energy"), "energy", "mdi:lightning-bolt", 2, [
    section(1, [statistics(t("heat_per_day"), heatRows(), ENERGY_DAYS)]),
    section(1, [statistics(t("electricity_per_day"),
      [row(e("total_electrical_energy_heat_generator"), t("heat_pump"))], ENERGY_DAYS)]),
    section(1, [statistics(t("starts_per_day"),
      [row(e("switching_cycles_heat_generator"), t("starts"))], ENERGY_DAYS)]),
    section(1, [entitiesCard(t("counters"), list(...COUNTER_KEYS).map((id) => row(id)))]),
  ]);

  // Settings per circuit, Smart Grid, and what isn't shown anywhere else.
  const keys = Object.keys(entities);
  const circuitCards = schema.circuits.map((hc) => {
    const ending = new RegExp(`(_hc${hc.number}|_heating_circuit_${hc.number})$`);
    const own = keys.filter((key) =>
      ending.test(key)
      && !key.startsWith("smart_grid_")
      && entities[key] !== hc.climate
      && !CIRCUIT_SKIPPED_KEYS.some((prefix) => key === `${prefix}${hc.number}`));
    const title = circuitName(hc);
    const patterns = [title, t("heating_circuit", { number: hc.number }),
      `heating circuit ${hc.number}`, `HC${hc.number}`];
    return entitiesCard(title, list(...own).map((id) => shortRow(id, patterns)));
  });
  const smartGrid = entitiesCard(t("smart_grid"),
    list(...keys.filter((key) => key.startsWith("smart_grid_")))
      .map((id) => shortRow(id, ["Smart Grid", "Smart-Grid-"])));
  // Built last, so it gets every entity that no other card shows.
  const rest = keys.map((key) => entities[key]).filter((id) => !used.has(id));
  const settings = view(t("settings"), "settings", "mdi:cog", 2, [
    ...circuitCards.map((card) => section(1, [card])),
    section(1, [smartGrid]),
    section(1, [entitiesCard(t("diagnostics"), rest.map((id) => row(id)))]),
    section(2, [{ type: "markdown", content: t("disabled_hint") }]),
  ]);

  return [overview, history, energy, settings];
}

// The entity's name without the device name in front, as HA shows it
// within the device.
function entityName(hass, id) {
  const state = hass.states[id];
  const name = (state && state.attributes.friendly_name) || id;
  const entry = hass.entities && hass.entities[id];
  const device = entry && entry.device_id && hass.devices && hass.devices[entry.device_id];
  const deviceName = device && (device.name_by_user || device.name);
  if (deviceName && name.startsWith(`${deviceName} `)) return name.slice(deviceName.length + 1);
  return name;
}

// The name without the given words (e.g. the card's title), with a capital
// first letter; the full name if nothing would be left.
function withoutTitle(name, patterns) {
  let short = name;
  for (const pattern of patterns) {
    const escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    // A prefix like "Smart-Grid-" ends within a word.
    const end = pattern.endsWith("-") ? "" : "(?=\\s|$)";
    short = short.replace(new RegExp(`(^|\\s)${escaped}${end}`, "gi"), " ");
  }
  short = short.replace(/\s+/g, " ").trim();
  return short ? short.charAt(0).toUpperCase() + short.slice(1) : name;
}

// The entities the system diagram card shows.
function schemaEntities(schema) {
  const values = (obj) => (obj ? Object.values(obj) : []);
  return [
    ...values(schema.heat_pump),
    ...schema.buffers.flatMap(values),
    ...values(schema.hot_water),
    ...schema.circuits.flatMap(values),
    schema.smart_grid_status,
  ].filter((id) => typeof id === "string" && id.includes("."));
}

// A sections view; the overview has room for four thermostats side by side,
// the other views for two charts or lists.
function view(title, path, icon, maxColumns, sections) {
  return { title, path, icon, type: "sections", max_columns: maxColumns, sections: sections.filter(Boolean) };
}

// A section with the given cards, each over its full width unless it sets
// its own; none if every card was skipped.
function section(columnSpan, cards) {
  const present = cards.filter(Boolean).map((card) => ({ grid_options: { columns: "full" }, ...card }));
  if (!present.length) return null;
  return { type: "grid", column_span: columnSpan, cards: present };
}

function graph(title, rows) {
  const entities = rows.filter(Boolean);
  if (!entities.length) return null;
  return { type: "history-graph", title, hours_to_show: HISTORY_HOURS, entities };
}

function statistics(title, rows, days) {
  const entities = rows.filter(Boolean);
  if (!entities.length) return null;
  return {
    type: "statistics-graph",
    title,
    entities,
    chart_type: "bar",
    period: "day",
    stat_types: ["change"],
    days_to_show: days,
  };
}

function entitiesCard(title, rows) {
  const entities = rows.filter(Boolean);
  if (!entities.length) return null;
  return { type: "entities", title, state_color: true, entities };
}

const EDITOR_SCHEMA = [
  { name: "entry_id", selector: { config_entry: { integration: "hoval_unofficial" } } },
];

class HovalDashboardStrategyEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    const lang = this._hass.locale && this._hass.locale.language;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (field) => text(lang, `editor_${field.name}`);
      this._form.computeHelper = (field) => text(lang, `editor_${field.name}_helper`);
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        const config = { ...this._config, ...ev.detail.value };
        if (!config.entry_id) delete config.entry_id;
        this._config = config;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = this._config;
    this._form.schema = EDITOR_SCHEMA;
  }
}

if (!customElements.get(STRATEGY_EDITOR_TAG)) {
  customElements.define(STRATEGY_EDITOR_TAG, HovalDashboardStrategyEditor);
}

if (!customElements.get(STRATEGY_TAG)) {
  customElements.define(STRATEGY_TAG, HovalDashboardStrategy);
  // Offered in the "Add dashboard" dialog.
  const lang = document.documentElement.lang || navigator.language;
  window.customStrategies = window.customStrategies || [];
  window.customStrategies.push({
    type: STRATEGY_TYPE,
    strategyType: "dashboard",
    name: text(lang, "name"),
    description: text(lang, "description"),
  });
}
