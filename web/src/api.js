// Тонкий клиент к FastAPI-бэкенду.
const j = (r) => {
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
};

// Ошибка с понятным текстом из тела ответа (для показа в UI).
const jOrErr = async (r) => {
  if (r.ok) return r.json();
  let msg = `${r.status}`;
  try {
    const b = await r.json();
    msg = b.detail || b.message || msg;
  } catch {}
  throw new Error(msg);
};

// Добавить поля маршрута проекта (реальный YouGile) в FormData.
const appendRoute = (fd, p) => {
  if (!p) return;
  fd.append("project", p.title);
  if (p.project_id) fd.append("yougile_project_id", p.project_id);
  if (p.board_id) fd.append("yougile_board_id", p.board_id);
  if (p.column_id) fd.append("yougile_column_id", p.column_id);
};

export const api = {
  // РЕАЛЬНЫЕ проекты YouGile (для выпадашек).
  yougileProjects: () => fetch("/api/yougile/projects").then(j),

  people: (project) =>
    fetch("/api/people" + (project ? `?project=${encodeURIComponent(project)}` : "")).then(j),

  addPerson: (body) =>
    fetch("/api/people", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(jOrErr),

  editPerson: (id, body) =>
    fetch(`/api/people/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(jOrErr),

  deletePerson: (id) =>
    fetch(`/api/people/${id}`, { method: "DELETE" }).then(jOrErr),

  simplenoteNotes: (tag) =>
    fetch("/api/simplenote/notes" + (tag ? `?tag=${encodeURIComponent(tag)}` : "")).then(jOrErr),

  simplenoteProcess: (note_ids, project) => {
    const body = { note_ids };
    if (project) {
      body.project = project.title;
      body.yougile_project_id = project.project_id;
      body.yougile_board_id = project.board_id;
      body.yougile_column_id = project.column_id;
    }
    return fetch("/api/simplenote/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(jOrErr);
  },

  upload: (file, project) => {
    const fd = new FormData();
    fd.append("file", file);
    appendRoute(fd, project);
    return fetch("/api/upload", { method: "POST", body: fd }).then(jOrErr);
  },

  uploadUrl: (url, project) => {
    const fd = new FormData();
    fd.append("url", url);
    appendRoute(fd, project);
    return fetch("/api/upload", { method: "POST", body: fd }).then(jOrErr);
  },

  status: (id) => fetch(`/api/meetings/${id}/status`).then(j),

  tasks: (id) => fetch(`/api/meetings/${id}/tasks`).then(j),

  approve: (id, tasks) =>
    fetch(`/api/meetings/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tasks }),
    }).then(j),
};
