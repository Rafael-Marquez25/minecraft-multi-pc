"use strict";

const byId = (id) => document.getElementById(id);
const settingsForm = byId("settings-form");
const addressForm = byId("address-form");
const recoverForm = byId("recover-form");
const gameState = byId("game-state");
const addressSelect = addressForm.elements.agent;
const gameConsole = byId("game-console");
const consoleOutput = byId("console-output");
const commandForm = byId("console-command");
const commandInput = commandForm.elements.command;
const commandButton = commandForm.querySelector("button");

const states = {
  idle: ["Parado", "Inicia la partida en el PC vinculado a este navegador."],
  starting: ["Iniciando", "El anfitrión prepara el mundo y arranca Minecraft."],
  running: ["En marcha", "Conéctate desde Minecraft Java a la dirección VPN que aparece abajo."],
  stopping: ["Deteniendo", "Minecraft está cerrando y guardando la partida en el anfitrión."],
  saving: ["Guardando", "El anfitrión está enviando el mundo al coordinador."],
  offline: ["Sin conexión", "La reserva sigue activa mientras el coordinador espera al anfitrión."],
  interrupted: ["Interrumpido", "Se requiere confirmar la recuperación."],
  failed: ["Error de inicio", "Revisa el error y vuelve a iniciar."],
};

const phases = {
  preparing: "Preparando el anfitrión",
  jar: "Descargando Minecraft de Mojang",
  world: "Descargando la copia del mundo",
  extracting: "Verificando y extrayendo el mundo",
  launching: "Arrancando Minecraft",
  stopping: "Cerrando Minecraft",
  packing: "Preparando la copia del mundo",
  uploading: "Subiendo la copia del mundo",
  verifying: "Verificando y guardando la copia",
  retrying: "Esperando para reintentar la subida",
};
const fallbackPhase = { starting: "preparing", stopping: "stopping", saving: "packing" };
const numbers = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 1 });
const consoleLimit = 2 * 64 * 1024;

let status = null;
let csrf = "";
let refreshNumber = 0;
let settingsDirty = false;
let addressDirty = false;
let signature = null;
let terminal = null;

function feedback(id, message = "", error = false) {
  const node = byId(id);
  node.textContent = message;
  node.classList.toggle("error", error);
  if (id === "connection-error") node.hidden = !message;
}

async function request(route, method = "GET", body) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrf && method !== "GET") headers["X-CSRF-Token"] = csrf;
  const response = await fetch("/api/v1" + route, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.ok) return response;
  const details = await response.json().catch(() => ({}));
  const error = new Error(details.detail || "El panel no pudo completar esta acción.");
  error.status = response.status;
  throw error;
}

async function api(route, method, body) {
  return (await request(route, method, body)).json();
}

function bytesText(size) {
  const units = ["B", "KiB", "MiB", "GiB"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return numbers.format(size) + " " + units[unit];
}

function renderProgress(data) {
  const phase = data.progress?.phase || fallbackPhase[data.state];
  byId("game-progress").hidden = !phases[phase];
  if (!phase) return;
  const label = byId("progress-label");
  if (label.textContent !== phases[phase]) label.textContent = phases[phase];
  const bar = byId("progress-bar");
  const count = byId("progress-count");
  const { bytes = 0, total } = data.progress || {};
  if (["jar", "world", "uploading"].includes(phase) && total) {
    const percent = Math.min(100, Math.floor(bytes / total * 100));
    bar.value = percent;
    count.textContent = `${percent} % · ${bytesText(bytes)} / ${bytesText(total)}`;
  } else {
    bar.removeAttribute("value");
    count.textContent = bytes && phase !== "verifying" ? bytesText(bytes) : "";
  }
}

function resetConsole(run, open = false) {
  terminal = run ? { run, bytes: new Uint8Array(), start: 0, end: 0, live: true } : null;
  gameConsole.hidden = !run;
  gameConsole.open = open;
  consoleOutput.textContent = "";
  byId("console-older").hidden = true;
  byId("console-latest").hidden = true;
  byId("console-connection").hidden = true;
  commandForm.reset();
  feedback("command-feedback");
}

function atConsoleBottom() {
  return consoleOutput.scrollHeight - consoleOutput.clientHeight - consoleOutput.scrollTop < 8;
}

async function loadConsole(mode = "update") {
  const view = terminal;
  if (!view || view.loading || !gameConsole.open) return;
  if (mode === "update" && (!view.live || (view.loaded && !atConsoleBottom()))) return;
  const position = mode === "older" ? `?before=${view.start}`
    : mode === "update" && view.loaded ? `?after=${view.end}` : "";
  view.loading = true;
  try {
    const response = await request(`/runs/${view.run}/console${position}`);
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (terminal !== view || !gameConsole.open) return;
    byId("console-connection").hidden = true;
    if (mode === "update" && view.loaded && (!bytes.length || !atConsoleBottom())) return;

    view.end = Number(response.headers.get("X-Log-End"));
    if (mode === "update" && view.loaded) {
      let cut = Math.max(0, view.bytes.length + bytes.length - consoleLimit);
      while (cut > 0 && (view.bytes[cut] & 0xc0) === 0x80) cut += 1;
      const retained = view.bytes.subarray(cut);
      view.bytes = new Uint8Array(retained.length + bytes.length);
      view.bytes.set(retained);
      view.bytes.set(bytes, retained.length);
    } else {
      view.bytes = bytes;
    }
    view.start = view.end - view.bytes.length;
    view.live = mode !== "older";
    view.loaded = true;

    consoleOutput.textContent = new TextDecoder().decode(view.bytes, { stream: true })
      || "Aún no hay salida de Minecraft.";
    consoleOutput.scrollTop = consoleOutput.scrollHeight;
    byId("console-older").hidden = view.start === 0;
    byId("console-latest").hidden = view.live;
  } catch {
    if (terminal === view) {
      const message = byId("console-connection");
      message.textContent = "Se interrumpió la lectura del registro. Vuelve a intentar la consulta.";
      message.hidden = false;
    }
  } finally {
    view.loading = false;
  }
}

function commandControls() {
  commandInput.disabled = !status?.console?.commands;
  commandButton.disabled = commandInput.disabled || Boolean(terminal?.pending || terminal?.sending);
}

async function checkCommand(view) {
  const pending = view.pending;
  if (!pending || pending.checking) return;
  pending.checking = true;
  try {
    const { status: outcome } = await api(`/runs/${view.run}/commands/${pending.id}`);
    if (view !== terminal || outcome === "pending") return;
    const written = outcome === "written";
    feedback("command-feedback", written
      ? (pending.stop ? "Detención solicitada." : "Comando entregado a Java.")
      : "No se pudo confirmar la entrega del comando.", !written);
    view.pending = null;
  } catch (error) {
    if (terminal === view && (error.status === 404 || Date.now() >= pending.deadline)) {
      feedback("command-feedback", "No se pudo confirmar la entrega del comando.", true);
      view.pending = null;
    }
  } finally {
    pending.checking = false;
    if (view === terminal) commandControls();
  }
}

function renderConsole(data) {
  const run = data.console?.run || null;
  if (run !== (terminal?.run || null)) resetConsole(run, data.state === "starting");
  commandControls();
  if (terminal) {
    loadConsole();
    checkCommand(terminal);
  }
}

function showLogin() {
  settingsDirty = addressDirty = false;
  status = null;
  resetConsole(null);
  csrf = "";
  byId("dashboard").hidden = true;
  byId("account-bar").hidden = true;
  byId("login-panel").hidden = false;
  byId("loading").hidden = true;
  byId("enrollment-wrap").hidden = true;
  const dialog = byId("recover-dialog");
  if (dialog.open) dialog.close();
}

function fillAddress() {
  const host = status.agents.find((entry) => entry.id === addressSelect.value);
  if (!addressDirty) addressForm.elements.address.value = host?.address || "";
  const locked = status.busy && host?.id === status.run?.agent;
  addressForm.elements.address.disabled = locked;
  addressForm.querySelector("button").disabled = locked;
}

function render(data) {
  const previousState = status?.state;
  status = data;
  csrf = data.csrf;
  byId("loading").hidden = true;
  byId("login-panel").hidden = true;
  byId("dashboard").hidden = false;
  byId("account-bar").hidden = false;
  byId("account-name").textContent = data.user;

  const [label, description] = states[data.state];
  if (previousState !== data.state) {
    gameState.className = "state " + data.state;
    gameState.firstElementChild.textContent = label;
    feedback("game-feedback");
  }
  byId("state-help").textContent = data.run?.reason || description;
  renderProgress(data);
  renderConsole(data);
  byId("game-version").textContent = "Java Edition · " + data.settings.version;
  byId("active-host").textContent = data.busy ? data.run.host : "—";
  byId("last-save").textContent = data.saved_at
    ? new Intl.DateTimeFormat("es-ES", { dateStyle: "short", timeStyle: "short" }).format(new Date(data.saved_at * 1000))
    : "Aún no hay una copia";
  byId("endpoint-wrap").hidden = !data.run?.endpoint;
  byId("endpoint").textContent = data.run?.endpoint || "";
  const paired = data.agents.find((host) => host.id === data.paired);
  byId("start").hidden = data.busy;
  byId("start").disabled = !paired?.online || !paired?.address || !data.settings.eula;
  byId("stop").hidden = data.state !== "running";
  byId("recover").hidden = !["offline", "interrupted"].includes(data.state);
  if (!data.busy && !data.run?.reason) {
    if (!paired) byId("state-help").textContent = "Vincula este navegador con un PC para iniciar.";
    else if (!paired.online) byId("state-help").textContent = "Inicia el agente del PC vinculado.";
    else if (!paired.address) byId("state-help").textContent = "Guarda la IPv4 VPN del PC vinculado.";
    else if (!data.settings.eula) byId("state-help").textContent = "Acepta y guarda la EULA en los ajustes.";
  }
  byId("device-name").textContent = paired?.name || "Sin vincular";
  byId("device-help").textContent = paired
    ? (paired.online ? "Agente conectado" : "Agente sin conexión") + (paired.address ? " · " + paired.address : "")
    : "Vincula este navegador para ejecutar Minecraft en su PC.";
  byId("device-pair").querySelector("summary").textContent = paired ? "Cambiar vinculación" : "Vincular navegador";

  if (!settingsDirty) {
    for (const [name, value] of Object.entries(data.settings)) {
      const input = settingsForm.elements.namedItem(name);
      if (input.type === "checkbox") input.checked = value;
      else input.value = value;
    }
  }
  byId("settings-fields").disabled = data.busy;
  byId("settings-help").hidden = !data.busy;
  settingsForm.elements.seed.readOnly = Boolean(data.saved_at);
  byId("seed-help").hidden = !data.saved_at;
  byId("network-range").textContent = "Red de pruebas " + data.network;
  addressForm.hidden = !data.agents.length;
  byId("no-agents").hidden = Boolean(data.agents.length);
  const newSignature = JSON.stringify(data.agents.map(({ id, name }) => [id, name]));
  if (signature !== newSignature) {
    const selected = addressSelect.value || data.paired;
    addressSelect.replaceChildren(...data.agents.map((host) => new Option(host.name, host.id)));
    if (data.agents.some((host) => host.id === selected)) addressSelect.value = selected;
    signature = newSignature;
  }
  fillAddress();
}

async function refresh() {
  const number = ++refreshNumber;
  const message = byId("connection-error");
  try {
    const data = await api("/status");
    if (number !== refreshNumber) return;
    render(data);
    message.hidden = true;
  } catch (error) {
    if (number !== refreshNumber) return;
    if (error.status === 401) {
      showLogin();
      message.hidden = true;
    } else {
      byId("loading").hidden = true;
      message.textContent = "No se pudo consultar el coordinador. El panel volverá a intentarlo.";
      message.hidden = false;
    }
  }
}

function action(node, eventName, messageId, callback) {
  node.addEventListener(eventName, async (event) => {
    event.preventDefault();
    if (node.dataset.busy) return;
    node.dataset.busy = "true";
    node.setAttribute("aria-busy", "true");
    feedback(messageId);
    try {
      await callback();
    } catch (error) {
      feedback(messageId, error.message, true);
    } finally {
      delete node.dataset.busy;
      node.removeAttribute("aria-busy");
    }
  });
}

action(byId("login-form"), "submit", "login-error", async () => {
  const form = byId("login-form");
  await api("/login", "POST", {
    name: form.elements.name.value,
    password: form.elements.password.value,
  });
  form.reset();
  await refresh();
});

action(byId("logout"), "click", "connection-error", async () => {
  await api("/logout", "POST");
  refreshNumber += 1;
  showLogin();
});

action(byId("start"), "click", "game-feedback", async () => {
  await api("/start", "POST");
  await refresh();
});

action(byId("stop"), "click", "game-feedback", async () => {
  await api("/runs/" + status.run.id + "/stop", "POST");
  await refresh();
});

gameConsole.addEventListener("toggle", () => loadConsole());
byId("console-older").addEventListener("click", () => loadConsole("older"));
byId("console-latest").addEventListener("click", () => loadConsole("latest"));
commandForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const view = terminal;
  const command = commandInput.value.trim();
  if (!view || commandButton.disabled || !command) return;
  view.sending = true;
  commandControls();
  feedback("command-feedback", "Enviando comando…");
  try {
    const { id } = await api(`/runs/${view.run}/commands`, "POST", { command });
    if (view !== terminal) return;
    if (commandInput.value.trim() === command) commandForm.reset();
    const normalized = command.replace(/^\/\s*/, "");
    const stop = ["stop", "minecraft:stop"].includes(normalized);
    view.pending = { id, stop, deadline: Date.now() + 35000 };
    checkCommand(view);
  } catch (error) {
    if (view === terminal) {
      const message = error.status && error.status < 500 ? error.message : "No se pudo confirmar el envío del comando.";
      feedback("command-feedback", message, true);
    }
  } finally {
    view.sending = false;
    if (view === terminal) commandControls();
  }
});

let recoveryRun = null;
byId("recover").addEventListener("click", () => {
  recoveryRun = status.run.id;
  recoverForm.reset();
  feedback("recover-feedback");
  byId("recover-copy").textContent = status.saved_at
    ? "El siguiente arranque usará la última copia del coordinador. Se pueden perder los cambios posteriores."
    : "Todavía no hay una copia central. El siguiente arranque generará un mundo nuevo.";
  byId("recover-dialog").showModal();
});
byId("cancel-recover").addEventListener("click", () => byId("recover-dialog").close());
action(recoverForm, "submit", "recover-feedback", async () => {
  await api("/runs/" + recoveryRun + "/recover", "POST", { confirmed: recoverForm.elements.confirmed.checked });
  byId("recover-dialog").close();
  await refresh();
  feedback("game-feedback", "Reserva liberada. Ya puedes iniciar desde otro PC.");
});

action(byId("pair-form"), "submit", "device-feedback", async () => {
  const form = byId("pair-form");
  await api("/pair", "POST", { code: form.elements.code.value.trim() });
  form.reset();
  byId("device-pair").open = false;
  await refresh();
  addressDirty = false;
  addressSelect.value = status.paired;
  fillAddress();
  feedback("device-feedback", "Navegador vinculado.");
});

action(byId("enrollment"), "click", "device-feedback", async () => {
  const { code } = await api("/enrollment-code", "POST");
  byId("enrollment-code").textContent = code;
  byId("enrollment-wrap").hidden = false;
  feedback("device-feedback", "Código válido durante 10 minutos y para un solo registro.");
});

function copy(button, source, messageId) {
  action(byId(button), "click", messageId, async () => {
    await navigator.clipboard.writeText(byId(source).textContent);
    feedback(messageId, "Copiado al portapapeles.");
  });
}
copy("copy-endpoint", "endpoint", "game-feedback");
copy("copy-enrollment", "enrollment-code", "device-feedback");

settingsForm.addEventListener("input", () => { settingsDirty = true; });
action(settingsForm, "submit", "settings-feedback", async () => {
  const input = settingsForm.elements;
  await api("/settings", "PUT", {
    version: input.version.value,
    memory_mib: input.memory_mib.valueAsNumber,
    port: input.port.valueAsNumber,
    seed: input.seed.value,
    difficulty: input.difficulty.value,
    max_players: input.max_players.valueAsNumber,
    eula: input.eula.checked,
  });
  settingsDirty = false;
  await refresh();
  feedback("settings-feedback", "Ajustes guardados.");
});

addressSelect.addEventListener("change", () => {
  addressDirty = false;
  fillAddress();
  feedback("network-feedback");
});
addressForm.elements.address.addEventListener("input", () => { addressDirty = true; });
action(addressForm, "submit", "network-feedback", async () => {
  await api("/agents/" + addressSelect.value + "/address", "PUT", {
    address: addressForm.elements.address.value.trim(),
  });
  addressDirty = false;
  await refresh();
  feedback("network-feedback", "Dirección guardada.");
});

refresh();
setInterval(refresh, 2500);
