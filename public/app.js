// Plain ES-module UI, no build step. All text is inserted with textContent (never innerHTML),
// so item titles/comments cannot inject markup. The server is the source of truth for permissions:
// buttons are hidden for convenience, but every action is re-checked server-side.

const STATUS = { open: "Open", in_progress: "In progress", awaiting_approval: "Awaiting approval", resolved: "Resolved", closed: "Closed" };
const TYPE = { incident: "Incident", customer_issue: "Customer issue", payment: "Payment", compliance: "Compliance", task: "Task" };
const PRIORITY = { urgent: "Urgent", high: "High", medium: "Medium", low: "Low" };
const STEP_LABEL = {
  "open>in_progress": "Start work",
  "in_progress>open": "Return to open",
  "in_progress>awaiting_approval": "Send for approval",
  "awaiting_approval>resolved": "Approve and resolve",
  "awaiting_approval>in_progress": "Reject, back to in progress",
  "resolved>closed": "Close",
  "resolved>in_progress": "Reopen",
};
const VIEWS = [
  ["assigned_to_me", "Assigned to me"],
  ["unassigned_urgent", "Unassigned urgent"],
  ["awaiting_my_approval", "Awaiting my approval"],
  ["all", "All items"],
];
const DEMO = [
  ["admin@demo.test", "Admin"],
  ["lead.pay@demo.test", "Lead (Payments)"],
  ["member.a@demo.test", "Member"],
  ["member.b@demo.test", "Member 2"],
  ["viewer.a@demo.test", "Viewer"],
  ["member.d@demo.test", "Other team"],
];

const state = {
  token: localStorage.getItem("token"),
  me: null,
  teams: [],
  view: "assigned_to_me",
  filters: { q: "", status: "", priority: "", type: "", team_id: "" },
  items: [],
  cursor: null,
  loading: false,
  selectedId: null,
  detail: null,
  activity: [],
  members: {}, // team_id -> [{id,name,role}]
  banner: null,
  pending: false,
};
let listRequestId = 0;
const els = {};

// ---------- tiny helpers ----------
const h = (tag, props = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of kids.flat()) e.append(c instanceof Node ? c : document.createTextNode(c ?? ""));
  return e;
};
const uuid = () => crypto.randomUUID();
const fmt = (iso) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

let toastTimer;
function toast(msg, bad = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), 5000);
}

/**
 * One place for all API calls. If an Idempotency-Key is supplied the call is safe to retry,
 * so a dropped connection is retried once with the SAME key (the server won't apply it twice).
 */
async function api(method, path, body, idemKey) {
  const headers = { "content-type": "application/json" };
  if (state.token) headers.authorization = "Bearer " + state.token;
  if (idemKey) headers["idempotency-key"] = idemKey;
  let res;
  for (let attempt = 0; ; attempt++) {
    try {
      res = await fetch("/api" + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
      break;
    } catch {
      if (!idemKey || attempt >= 1) throw Object.assign(new Error("Network problem. Check your connection and try again."), { status: 0 });
    }
  }
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    logout();
    throw Object.assign(new Error("Your session expired. Please sign in again."), { status: 401 });
  }
  if (!res.ok) throw Object.assign(new Error(data.error || "Request failed"), { status: res.status, data });
  return data;
}

// ---------- login ----------
function logout() {
  localStorage.removeItem("token");
  Object.assign(state, { token: null, me: null, items: [], detail: null, selectedId: null });
  renderLogin();
}

function renderLogin(message = "") {
  const app = document.getElementById("app");
  const email = h("input", { type: "email", name: "email", autocomplete: "username", required: true, value: "member.a@demo.test" });
  const password = h("input", { type: "password", name: "password", autocomplete: "current-password", required: true, value: "password123" });
  const err = h("p", { class: "error", role: "alert" }, message);
  const form = h(
    "form",
    {
      onsubmit: async (ev) => {
        ev.preventDefault();
        try {
          const res = await fetch("/auth/login", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ email: email.value, password: password.value }) });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || "Sign in failed");
          state.token = data.token;
          localStorage.setItem("token", data.token);
          boot();
        } catch (e) {
          err.textContent = e.message;
        }
      },
    },
    h("label", {}, "Email", email),
    h("label", {}, "Password", password),
    h("button", { class: "primary", type: "submit" }, "Sign in"),
    err,
  );
  app.replaceChildren(
    h(
      "div",
      { class: "login" },
      h("h1", {}, "Ops Desk"),
      h("p", { class: "who" }, "Demo accounts (password: password123). Pick one to fill the form:"),
      h("div", { class: "demo-users" }, DEMO.map(([e, label]) => h("button", { type: "button", onclick: () => (email.value = e) }, label))),
      form,
    ),
  );
}

// ---------- shell ----------
async function boot() {
  try {
    const me = await api("GET", "/me");
    state.me = me.user;
    state.teams = me.teams;
  } catch {
    return renderLogin();
  }
  renderShell();
  loadList(true);
  pollNotifications();
}

function renderShell() {
  const app = document.getElementById("app");
  els.notifBtn = h("button", { onclick: showNotifications }, "Notifications");
  els.tabs = h("div", { class: "tabs", role: "group", "aria-label": "Views" });
  els.list = h("div", { class: "list", "aria-live": "polite" });
  els.detail = h("aside", { class: "detail", "aria-label": "Item details" });

  const search = h("input", { type: "search", placeholder: "Search title or description", "aria-label": "Search", oninput: debounce((e) => { state.filters.q = e.target.value.trim(); loadList(true); }, 250) });
  const sel = (key, label, options) =>
    h("select", { "aria-label": label, onchange: (e) => { state.filters[key] = e.target.value; loadList(true); } }, h("option", { value: "" }, label), Object.entries(options).map(([v, t]) => h("option", { value: v }, t)));
  const filters = h(
    "div",
    { class: "filters" },
    search,
    sel("status", "Any status", STATUS),
    sel("priority", "Any priority", PRIORITY),
    sel("type", "Any type", TYPE),
    sel("team_id", "All my teams", Object.fromEntries(state.teams.map((t) => [t.id, t.name]))),
  );

  app.replaceChildren(
    h(
      "header",
      { class: "top" },
      h("h1", {}, "Ops Desk"),
      h("span", { class: "who" }, state.me.name),
      h("button", { class: "primary", onclick: openCreate }, "New item"),
      els.notifBtn,
      h("button", { onclick: logout }, "Sign out"),
    ),
    h("main", { class: "layout" }, h("section", {}, els.tabs, filters, els.list), els.detail),
  );
  renderTabs();
  renderDetail();
}

function renderTabs() {
  els.tabs.replaceChildren(
    ...VIEWS.map(([key, label]) =>
      h("button", { "aria-pressed": String(state.view === key), onclick: () => { state.view = key; renderTabs(); loadList(true); } }, label),
    ),
    h("button", { class: "link", style: "margin-left:8px", onclick: () => loadList(true) }, "Refresh"),
  );
}

const debounce = (fn, ms) => {
  let t;
  return (...a) => (clearTimeout(t), (t = setTimeout(() => fn(...a), ms)));
};

// ---------- list (server-side filter, search, cursor paging) ----------
async function loadList(reset) {
  const myReq = ++listRequestId;
  if (reset) state.cursor = null;
  const p = new URLSearchParams({ limit: "25" });
  if (state.view !== "all") p.set("view", state.view);
  for (const [k, v] of Object.entries(state.filters)) if (v) p.set(k, v);
  if (!reset && state.cursor) p.set("cursor", state.cursor);
  state.loading = true;
  try {
    const data = await api("GET", "/items?" + p);
    if (myReq !== listRequestId) return; // a newer request superseded this one
    state.items = reset ? data.items : state.items.concat(data.items);
    state.cursor = data.next_cursor;
  } catch (e) {
    if (e.status !== 401) toast(e.message, true);
  } finally {
    if (myReq === listRequestId) state.loading = false;
  }
  renderList();
}

function renderList() {
  if (!els.list) return;
  if (!state.items.length) {
    els.list.replaceChildren(h("div", { class: "empty" }, state.loading ? "Loading..." : "Nothing here. Try another view or clear the filters."));
    return;
  }
  const rows = state.items.map((it) =>
    h(
      "div",
      { class: "row", role: "button", tabindex: "0", "aria-selected": String(it.id === state.selectedId), onclick: () => openItem(it.id), onkeydown: (e) => e.key === "Enter" && openItem(it.id) },
      h("span", { class: "title" }, it.title),
      h("span", {}, h("span", { class: `badge p-${it.priority}` }, PRIORITY[it.priority])),
      h("span", { class: "meta" }, `${STATUS[it.status]} · ${it.team_name} · ${it.assignee_name ? "Assigned to " + it.assignee_name : "Unassigned"}${it.due_date ? " · Due " + it.due_date : ""}`),
    ),
  );
  els.list.replaceChildren(...rows, state.cursor ? h("div", { class: "more" }, h("button", { onclick: () => loadList(false) }, "Load more")) : "");
}

// ---------- detail ----------
async function openItem(id) {
  state.selectedId = id;
  state.banner = null;
  renderList();
  await loadDetail(id);
}

async function loadDetail(id) {
  try {
    const [detail, act] = await Promise.all([api("GET", `/items/${id}`), api("GET", `/items/${id}/activity?limit=50`)]);
    if (state.selectedId !== id) return;
    state.detail = detail;
    state.activity = act.events;
    if (!state.members[detail.team_id]) state.members[detail.team_id] = (await api("GET", `/teams/${detail.team_id}/members`)).members;
    syncRow(detail);
  } catch (e) {
    if (e.status === 404) {
      state.detail = null;
      state.selectedId = null;
      toast("That item is no longer available to you.", true);
    } else if (e.status !== 401) toast(e.message, true);
  }
  renderDetail();
}

/** Copy fresh server data into the matching list row. */
function syncRow(item) {
  const i = state.items.findIndex((r) => r.id === item.id);
  if (i >= 0) state.items[i] = { ...state.items[i], ...item };
  renderList();
}

function nameOf(teamId, userId) {
  return (state.members[teamId] || []).find((m) => m.id === userId)?.name ?? "someone";
}

function describe(ev, teamId) {
  const d = ev.data;
  switch (ev.kind) {
    case "created": return "created this item";
    case "status_changed": return `moved it from ${STATUS[d.from]} to ${STATUS[d.to]}` + (d.note ? ` (${d.note})` : "");
    case "priority_changed": return `changed priority from ${PRIORITY[d.from]} to ${PRIORITY[d.to]}`;
    case "assignee_changed":
      if (d.via === "claim") return "claimed this item";
      return d.to ? `assigned it to ${nameOf(teamId, d.to)}` : "unassigned it";
    case "edited": return `edited ${d.fields.join(", ").replaceAll("_", " ")}`;
    default: return ev.kind;
  }
}

function renderDetail() {
  const box = els.detail;
  const it = state.detail;
  if (!it) {
    box.replaceChildren(h("div", { class: "empty" }, "Select an item to see its details and history."));
    return;
  }
  const can = (a) => it.allowed_actions.includes(a);
  const open = it.status !== "closed";
  const kids = [h("h2", {}, it.title), h("div", { class: "sub" }, h("span", { class: `badge p-${it.priority}` }, PRIORITY[it.priority]), " ", h("span", { class: "badge status" }, STATUS[it.status]), ` ${TYPE[it.type]}`)];

  if (state.banner) {
    kids.push(h("div", { class: "banner", role: "alert" }, state.banner + " ", h("button", { class: "link", onclick: () => { state.banner = null; loadDetail(it.id); } }, "Reload latest")));
  }
  if (it.description) kids.push(h("p", { class: "desc" }, it.description));
  kids.push(
    h(
      "dl",
      { class: "facts" },
      h("dt", {}, "Assignee"), h("dd", {}, it.assignee_name ?? "Unassigned"),
      h("dt", {}, "Team"), h("dd", {}, it.team_name),
      h("dt", {}, "Created by"), h("dd", {}, `${it.created_by_name} on ${fmt(it.created_at)}`),
      h("dt", {}, "Due"), h("dd", {}, it.due_date ?? "No due date"),
      h("dt", {}, "Version"), h("dd", {}, String(it.version)),
    ),
  );

  const actions = [];
  if (can("claim") && !it.assignee_id && !["resolved", "closed"].includes(it.status)) {
    actions.push(h("button", { class: "primary", disabled: state.pending, onclick: () => doClaim(it) }, "Claim"));
  }
  for (const to of it.next_statuses) {
    const label = STEP_LABEL[`${it.status}>${to}`] ?? STATUS[to];
    actions.push(h("button", { disabled: state.pending, onclick: () => doTransition(it, to) }, label));
  }
  if (actions.length) kids.push(h("div", { class: "actions" }, actions));

  if ((can("edit") || can("assign")) && open) kids.push(editForm(it, can));
  if (can("comment")) kids.push(commentForm(it));

  kids.push(
    h("h3", {}, "History"),
    h(
      "ol",
      { class: "timeline" },
      state.activity.map((ev) =>
        h(
          "li",
          { class: ev.kind === "comment" ? "comment" : "" },
          h("div", {}, h("strong", {}, ev.actor_name), ev.kind === "comment" ? " commented" : " " + describe(ev, it.team_id)),
          ev.kind === "comment" ? h("div", { class: "quote" }, ev.data.text) : "",
          h("time", { datetime: ev.created_at }, fmt(ev.created_at)),
        ),
      ),
    ),
  );
  box.replaceChildren(...kids);
}

function editForm(it, can) {
  const title = h("input", { value: it.title, maxlength: "200", required: true, disabled: !can("edit") });
  const desc = h("textarea", { disabled: !can("edit") });
  desc.value = it.description;
  const priority = h("select", { disabled: !can("edit") }, Object.entries(PRIORITY).map(([v, t]) => h("option", { value: v, selected: v === it.priority }, t)));
  const due = h("input", { type: "date", value: it.due_date ?? "", disabled: !can("edit") });
  const workers = (state.members[it.team_id] || []).filter((m) => m.role !== "viewer");
  const assignee = h("select", {}, h("option", { value: "" }, "Unassigned"), workers.map((m) => h("option", { value: m.id, selected: m.id === it.assignee_id }, m.name)));
  const form = h(
    "form",
    {
      class: "edit",
      onsubmit: async (ev) => {
        ev.preventDefault();
        const body = { version: it.version };
        if (can("edit")) Object.assign(body, { title: title.value.trim(), description: desc.value, priority: priority.value, due_date: due.value || null });
        if (can("assign") && (assignee.value || null) !== it.assignee_id) body.assignee_id = assignee.value || null;
        await runAction(it, null, () => api("PATCH", `/items/${it.id}`, body, uuid()), "Saved");
      },
    },
    h("h3", {}, "Edit"),
    h("label", {}, "Title", title),
    h("label", {}, "Description", desc),
    h("div", { class: "grid" }, h("label", {}, "Priority", priority), h("label", {}, "Due date", due)),
    can("assign") ? h("label", {}, "Assignee", assignee) : "",
    h("button", { type: "submit", disabled: state.pending }, "Save changes"),
  );
  return form;
}

function commentForm(it) {
  const text = h("textarea", { placeholder: "Add a comment", "aria-label": "Comment" });
  return h(
    "form",
    {
      class: "edit",
      onsubmit: async (ev) => {
        ev.preventDefault();
        if (!text.value.trim()) return;
        try {
          await api("POST", `/items/${it.id}/comments`, { text: text.value.trim() }, uuid());
          await loadDetail(it.id);
        } catch (e) {
          toast(e.message, true);
        }
      },
    },
    text,
    h("button", { type: "submit" }, "Comment"),
  );
}

// ---------- actions with optimistic UI + rollback ----------
function doClaim(it) {
  return runAction(
    it,
    { assignee_id: state.me.id, assignee_name: state.me.name },
    () => api("POST", `/items/${it.id}/claim`, null, uuid()),
    "Claimed",
  );
}
function doTransition(it, to) {
  return runAction(
    it,
    { status: to },
    () => api("POST", `/items/${it.id}/transition`, { to, version: it.version }, uuid()),
    `Moved to ${STATUS[to]}`,
  );
}

/**
 * Show the expected result immediately; if the server rejects it (409 conflict, 403, ...),
 * restore the previous state and explain why. On 409 the server also returns the latest
 * item, so we show a "this item changed, reload" banner instead of leaving stale data on screen.
 */
async function runAction(it, optimisticChange, request, successMsg) {
  if (state.pending) return; // blocks double-clicks; the Idempotency-Key covers retries
  const before = { detail: state.detail, items: state.items };
  state.pending = true;
  if (optimisticChange) {
    state.detail = { ...state.detail, ...optimisticChange };
    state.items = state.items.map((r) => (r.id === it.id ? { ...r, ...optimisticChange } : r));
    state.banner = null;
  }
  renderList();
  renderDetail();
  try {
    await request();
    toast(successMsg);
    state.banner = null;
  } catch (e) {
    state.detail = before.detail; // roll back
    state.items = before.items;
    if (e.status === 409) state.banner = e.message;
    toast(e.message, true);
  } finally {
    state.pending = false;
    await loadDetail(it.id); // reconcile with whatever the server actually holds
  }
}

// ---------- create ----------
function openCreate() {
  const writable = state.teams.filter((t) => t.role !== "viewer");
  if (!writable.length) return toast("You only have Viewer access, so you cannot create items.", true);
  let key = uuid(); // one key per dialog: double-clicking Create or a retry cannot make two items
  const f = {
    team: h("select", {}, writable.map((t) => h("option", { value: t.id }, `${t.name} (${t.role})`))),
    title: h("input", { required: true, maxlength: "200" }),
    description: h("textarea", {}),
    type: h("select", {}, Object.entries(TYPE).map(([v, t]) => h("option", { value: v }, t))),
    priority: h("select", {}, Object.entries(PRIORITY).map(([v, t]) => h("option", { value: v, selected: v === "medium" }, t))),
    due: h("input", { type: "date" }),
  };
  const err = h("p", { class: "error", role: "alert" });
  const dlg = h(
    "dialog",
    {},
    h(
      "form",
      {
        method: "dialog",
        onsubmit: async (ev) => {
          ev.preventDefault();
          const btn = ev.submitter;
          btn.disabled = true;
          try {
            const item = await api("POST", "/items", { title: f.title.value.trim(), description: f.description.value, type: f.type.value, priority: f.priority.value, team_id: f.team.value, due_date: f.due.value || null }, key);
            dlg.close();
            dlg.remove();
            toast("Item created");
            state.view = "all";
            renderTabs();
            await loadList(true);
            openItem(item.id);
          } catch (e) {
            err.textContent = e.message;
            btn.disabled = false;
            if (e.status === 422) key = uuid();
          }
        },
      },
      h("h2", {}, "New item"),
      h("label", {}, "Team", f.team),
      h("label", {}, "Title", f.title),
      h("label", {}, "Description", f.description),
      h("div", { class: "edit grid", style: "border:0;padding:0;margin:0" }, h("label", {}, "Type", f.type), h("label", {}, "Priority", f.priority)),
      h("label", {}, "Due date", f.due),
      err,
      h("div", { class: "dialog-actions" }, h("button", { type: "button", onclick: () => { dlg.close(); dlg.remove(); } }, "Cancel"), h("button", { class: "primary", type: "submit" }, "Create item")),
    ),
  );
  document.body.append(dlg);
  dlg.showModal();
}

// ---------- notifications (written by the background worker) ----------
async function pollNotifications() {
  clearInterval(pollNotifications.timer);
  const tick = async () => {
    if (!state.token) return;
    try {
      const { notifications } = await api("GET", "/notifications");
      state.notifications = notifications;
      const seen = Number(localStorage.getItem("seenNotif:" + state.me.id) || 0);
      const unseen = notifications.filter((n) => n.id > seen).length;
      if (els.notifBtn) els.notifBtn.textContent = unseen ? `Notifications (${unseen})` : "Notifications";
    } catch {}
  };
  tick();
  pollNotifications.timer = setInterval(tick, 10000);
}

function showNotifications() {
  const list = state.notifications || [];
  if (list.length) localStorage.setItem("seenNotif:" + state.me.id, String(list[0].id));
  els.notifBtn.textContent = "Notifications";
  const dlg = h(
    "dialog",
    {},
    h("h2", {}, "Notifications"),
    list.length
      ? h("ul", {}, list.map((n) => h("li", {}, h("button", { class: "link", onclick: () => { dlg.close(); dlg.remove(); if (n.item_id) openItem(n.item_id); } }, n.message), ` ${fmt(n.created_at)}`)))
      : h("p", { class: "who" }, "Nothing yet. You get one when someone assigns you an item or resolves one you created."),
    h("div", { class: "dialog-actions" }, h("button", { onclick: () => { dlg.close(); dlg.remove(); } }, "Close")),
  );
  document.body.append(dlg);
  dlg.showModal();
}

// ---------- start ----------
if (state.token) boot();
else renderLogin();
