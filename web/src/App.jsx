import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";

// Какой флаг подсвечивает какое поле.
const FIELD_FLAG = {
  assignee: "assignee_unmatched",
  controller: "controller_unmatched",
  due_date: "due_invented",
};
const HARD_FLAGS = new Set(["no_grounding"]);
const PRIORITIES = ["low", "medium", "high", "urgent"];

const STEPS = [
  { key: "transcribing", label: "Транскрибируется" },
  { key: "processing", label: "Разбирается на задачи" },
  { key: "awaiting_review", label: "Готово к ревью" },
];

export default function App() {
  const [screen, setScreen] = useState("upload");
  const [meetingId, setMeetingId] = useState(null);
  const [status, setStatus] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [sent, setSent] = useState(null);
  const [error, setError] = useState(null);

  // --- фаза 1: опрос статуса ---
  useEffect(() => {
    if (screen !== "processing" || meetingId == null) return;
    let stop = false;
    const tick = async () => {
      try {
        const s = await api.status(meetingId);
        if (stop) return;
        setStatus(s.status);
        if (s.status === "awaiting_review") {
          const t = await api.tasks(meetingId);
          setTasks(t.tasks.map((x) => ({ ...x, _approved: true })));
          setScreen("review");
          return;
        }
        if (s.status === "error") {
          setError("Разбор завершился ошибкой. Проверьте логи бэкенда.");
          return;
        }
        setTimeout(tick, 1500);
      } catch (e) {
        if (!stop) setTimeout(tick, 1500);
      }
    };
    tick();
    return () => { stop = true; };
  }, [screen, meetingId]);

  const startUpload = async (source, project) => {
    setError(null);
    try {
      const { meeting_id } = source.url
        ? await api.uploadUrl(source.url, project)
        : await api.upload(source.file, project);
      setMeetingId(meeting_id);
      setStatus("uploaded");
      setScreen("processing");
    } catch (e) {
      setError(String(e.message || e));
    }
  };

  const send = async () => {
    const approved = tasks.filter((t) => t._approved).map(({ _approved, ...t }) => t);
    setError(null);
    try {
      const res = await api.approve(meetingId, approved);
      setStatus(res.status); // синхронизируем статус в шапке (awaiting_review → done)
      setSent(res);
      setScreen("sent");
    } catch (e) {
      setError(String(e.message || e));
    }
  };

  return (
    <>
      <Masthead meetingId={meetingId} status={status} />
      <main className="wrap">
        {error && <p className="err">⚠ {error}</p>}
        {screen === "upload" && <Upload onStart={startUpload} />}
        {screen === "processing" && <Processing status={status} />}
        {screen === "review" && (
          <Review tasks={tasks} setTasks={setTasks} onSend={send} />
        )}
        {screen === "sent" && <Sent result={sent} />}
      </main>
    </>
  );
}

function Masthead({ meetingId, status }) {
  return (
    <header className="masthead">
      <div className="masthead-inner">
        <div className="wordmark">
          Созвон <span className="arrow">→</span> Задачи
        </div>
        <div className="masthead-meta">
          <div className="eyebrow">ревью-деск</div>
          {meetingId != null && (
            <div className="eyebrow">созвон #{meetingId} · {status || "—"}</div>
          )}
        </div>
      </div>
    </header>
  );
}

function Upload({ onStart }) {
  const [file, setFile] = useState(null);
  const [url, setUrl] = useState("");
  const [projects, setProjects] = useState([]); // [{project_id, title, board_id, column_id}]
  const [projectTitle, setProjectTitle] = useState("");
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [over, setOver] = useState(false);
  const [teamOpen, setTeamOpen] = useState(false);
  const inputRef = useRef();

  useEffect(() => {
    // Реальные проекты YouGile (живой API) для выпадашки.
    api.yougileProjects().then((p) => {
      setProjects(p);
      if (p[0]) setProjectTitle(p[0].title);
    }).catch(() => {}).finally(() => setProjectsLoading(false));
  }, []);

  const onDrop = (e) => {
    e.preventDefault();
    setOver(false);
    if (e.dataTransfer.files[0]) { setFile(e.dataTransfer.files[0]); setUrl(""); }
  };

  const selectedProject = projects.find((p) => p.title === projectTitle) || null;

  return (
    <section>
      <div className="upload-head">
        <div className="eyebrow">шаг 1 · загрузка</div>
        <button className="btn-ghost btn-small" onClick={() => setTeamOpen(true)}>
          ☰ База команды
        </button>
      </div>

      <div
        className={"dropzone" + (over ? " over" : "")}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
      >
        <div className="eyebrow">аудио или транскрипт</div>
        <h2>Перетащите запись созвона</h2>
        <p>или нажмите, чтобы выбрать файл · mp3, m4a, wav, txt, md</p>
        {file && <div className="filename">{file.name}</div>}
      </div>
      {/* input — СИБЛИНГ дропзоны, не потомок: иначе input.click() всплывает обратно
          в onClick дропзоны и вызывает .click() повторно → Chrome глушит диалог. */}
      <input
        ref={inputRef}
        type="file"
        accept=".mp3,.mp4,.m4a,.wav,.webm,.txt,.md"
        style={{ display: "none" }}
        onChange={(e) => { setFile(e.target.files[0] || null); setUrl(""); }}
      />

      <div className="url-row">
        <span className="eyebrow">или вставьте ссылку</span>
        <input
          className="text-input url-input"
          placeholder="https://… · Яндекс.Диск · Google Drive"
          value={url}
          onChange={(e) => { setUrl(e.target.value); if (e.target.value) setFile(null); }}
        />
      </div>

      <div className="field-row">
        <div className="field">
          <label htmlFor="proj">Проект из YouGile (маршрут)</label>
          <select id="proj" value={projectTitle} onChange={(e) => setProjectTitle(e.target.value)}>
            {projectsLoading && <option value="">загрузка из YouGile…</option>}
            {!projectsLoading && projects.length === 0 && <option value="">— нет проектов —</option>}
            {projects.map((p) => (
              <option key={p.project_id} value={p.title}>{p.title}</option>
            ))}
          </select>
        </div>
        {url.trim() ? (
          <button className="btn" onClick={() => onStart({ url: url.trim() }, selectedProject)}>
            Скачать и разобрать
          </button>
        ) : (
          <button className="btn" disabled={!file} onClick={() => onStart({ file }, selectedProject)}>
            Разобрать созвон
          </button>
        )}
      </div>

      {teamOpen && <TeamModal projects={projects} defaultProject={projectTitle} onClose={() => setTeamOpen(false)} />}
    </section>
  );
}

function TeamModal({ projects, defaultProject, onClose }) {
  const [people, setPeople] = useState([]);
  const [name, setName] = useState("");
  const [specialty, setSpecialty] = useState("");
  const [project, setProject] = useState(defaultProject || (projects[0]?.title ?? ""));
  const [warning, setWarning] = useState(null);
  const [busy, setBusy] = useState(false);
  const [editId, setEditId] = useState(null);

  const reload = () => api.people().then(setPeople).catch(() => {});
  useEffect(() => { reload(); }, []);

  const add = async () => {
    if (!name.trim()) return;
    setBusy(true);
    setWarning(null);
    try {
      const res = await api.addPerson({ name: name.trim(), specialty: specialty.trim() || null, project: project || null });
      setWarning(res.warning); // null если сматчился
      setName(""); setSpecialty("");
      await reload();
    } catch (e) {
      setWarning(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const saveEdit = async (id, patch) => {
    setWarning(null);
    try {
      const res = await api.editPerson(id, patch);
      setWarning(res.warning);
      setEditId(null);
      await reload();
    } catch (e) {
      setWarning(String(e.message || e));
    }
  };

  const remove = async (id, who) => {
    if (!window.confirm(`Удалить «${who}» из базы команды?`)) return;
    setWarning(null);
    try {
      await api.deletePerson(id);
      await reload();
    } catch (e) {
      setWarning(String(e.message || e));
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>База команды</h3>
          <button className="x" onClick={onClose} aria-label="Закрыть">×</button>
        </div>

        <ul className="team-list">
          {people.length === 0 && <li className="empty">Пока никого. Добавьте первого сотрудника.</li>}
          {people.map((p) =>
            editId === p.id ? (
              <PersonEditRow key={p.id} person={p} projects={projects}
                onSave={(patch) => saveEdit(p.id, patch)} onCancel={() => setEditId(null)} />
            ) : (
              <li key={p.id}>
                <span className="t-name">{p.name}</span>
                <span className="t-spec">{p.specialty || "—"}</span>
                <span className="t-proj">{p.project || "—"}</span>
                <span className={"t-id " + (p.matched ? "ok" : "none")}>
                  {p.matched ? "YouGile ✓" : "id не найден"}
                </span>
                <span className="t-actions">
                  <button className="row-btn" title="Редактировать" onClick={() => { setEditId(p.id); setWarning(null); }}>✎</button>
                  <button className="row-btn del" title="Удалить" onClick={() => remove(p.id, p.name)}>🗑</button>
                </span>
              </li>
            )
          )}
        </ul>

        <div className="add-form">
          <div className="eyebrow">добавить сотрудника</div>
          <div className="add-grid">
            <input className="text-input" placeholder="Имя (напр. Анна)" value={name} onChange={(e) => setName(e.target.value)} />
            <input className="text-input" placeholder="Специальность (напр. СММ)" value={specialty} onChange={(e) => setSpecialty(e.target.value)} />
            <select value={project} onChange={(e) => setProject(e.target.value)}>
              {projects.map((p) => <option key={p.project_id} value={p.title}>{p.title}</option>)}
            </select>
            <button className="btn btn-send" disabled={busy || !name.trim()} onClick={add}>Добавить</button>
          </div>
          {warning && <p className="warn-msg">⚠ {warning}</p>}
        </div>
      </div>
    </div>
  );
}

function PersonEditRow({ person, projects, onSave, onCancel }) {
  const [name, setName] = useState(person.name || "");
  const [specialty, setSpecialty] = useState(person.specialty || "");
  const [project, setProject] = useState(person.project || (projects[0]?.title ?? ""));

  return (
    <li className="editing">
      <input className="text-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Имя" />
      <input className="text-input" value={specialty} onChange={(e) => setSpecialty(e.target.value)} placeholder="Специальность" />
      <select value={project} onChange={(e) => setProject(e.target.value)}>
        <option value="">— проект —</option>
        {projects.map((p) => <option key={p.project_id} value={p.title}>{p.title}</option>)}
      </select>
      <span className="t-actions">
        <button className="row-btn save" title="Сохранить" disabled={!name.trim()}
          onClick={() => onSave({ name: name.trim(), specialty: specialty.trim() || null, project: project || null })}>✓</button>
        <button className="row-btn" title="Отмена" onClick={onCancel}>×</button>
      </span>
    </li>
  );
}

function Processing({ status }) {
  const idx = STEPS.findIndex((s) => s.key === status);
  const active = idx === -1 ? 0 : idx;
  return (
    <section className="proc">
      <div className="eyebrow">шаг 2 · обработка</div>
      <div className="steps">
        {STEPS.map((s, i) => (
          <div
            key={s.key}
            className={"step " + (i < active ? "done" : i === active ? "active" : "")}
          >
            {i === active ? <div className="spin" /> : <div className="dot" />}
            {s.label}
          </div>
        ))}
      </div>
    </section>
  );
}

function Review({ tasks, setTasks, onSend }) {
  const flaggedCount = tasks.filter((t) => (t.validator_flags || []).length).length;
  const approvedCount = tasks.filter((t) => t._approved).length;

  const update = (i, patch) =>
    setTasks(tasks.map((t, j) => (j === i ? { ...t, ...patch } : t)));

  return (
    <section>
      <div className="review-head">
        <div>
          <div className="eyebrow">шаг 3 · ревью · запись только по одобрению</div>
          <h1>Черновики задач</h1>
        </div>
        <div className="tally">
          {tasks.length} задач · <b>{flaggedCount}</b> с флагами
        </div>
      </div>

      {tasks.map((t, i) => (
        <TaskCard key={t.internal_id || i} task={t} onChange={(p) => update(i, p)} />
      ))}

      <div className="actionbar">
        <div className="actionbar-inner">
          <div className="count">
            одобрено <b>{approvedCount}</b> из {tasks.length}
          </div>
          <button className="btn btn-send" disabled={approvedCount === 0} onClick={onSend}>
            Отправить одобренные в YouGile · {approvedCount}
          </button>
        </div>
      </div>
    </section>
  );
}

function TaskCard({ task, onChange }) {
  const [showSrc, setShowSrc] = useState(false);
  const flags = task.validator_flags || [];
  const hasFlag = (f) => flags.includes(f);
  const fieldFlagged = (field) => hasFlag(FIELD_FLAG[field]);
  const noGrounding = hasFlag("no_grounding");

  return (
    <article className={"card" + (task._approved ? " approved" : " dropped")}>
      <div className="card-top">
        <div className="approve-box">
          <input
            type="checkbox"
            checked={!!task._approved}
            onChange={(e) => onChange({ _approved: e.target.checked })}
            aria-label="Одобрить задачу"
          />
        </div>
        <input
          className="title-input"
          value={task.title || ""}
          onChange={(e) => onChange({ title: e.target.value })}
        />
      </div>

      <div className="meta">
        <Field label="Исполнитель" flagged={fieldFlagged("assignee")}>
          <input value={task.assignee || ""} onChange={(e) => onChange({ assignee: e.target.value })} />
          <IdPill id={task.assignee_id} />
        </Field>
        <Field label="Контролёр" flagged={fieldFlagged("controller")}>
          <input value={task.controller || ""} onChange={(e) => onChange({ controller: e.target.value })} />
          <IdPill id={task.controller_id} />
        </Field>
        <Field label="Срок" flagged={fieldFlagged("due_date")}>
          <input
            placeholder="—"
            value={task.due_date || ""}
            onChange={(e) => onChange({ due_date: e.target.value || null })}
          />
        </Field>
        <Field label="Приоритет">
          <select value={task.priority || "medium"} onChange={(e) => onChange({ priority: e.target.value })}>
            {PRIORITIES.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </Field>
      </div>

      <div className="card-foot">
        <div className="flags">
          {flags.length === 0 && <span className="flag clean">без флагов</span>}
          {flags.map((f) => (
            <span key={f} className={"flag" + (HARD_FLAGS.has(f) ? " hard" : "")}>{f}</span>
          ))}
        </div>
        <button className="src-toggle" onClick={() => setShowSrc((v) => !v)}>
          {showSrc ? "скрыть источник" : "▸ источник"}
        </button>
        {showSrc && (
          <div className="evidence" style={noGrounding ? { borderLeftColor: "var(--red-ink)" } : null}>
            <span className="lbl">фрагмент транскрипта</span>
            {task.source ? `«${task.source}»` : "— источник отсутствует —"}
          </div>
        )}
      </div>
    </article>
  );
}

function Field({ label, flagged, children }) {
  return (
    <label className={"meta-field" + (flagged ? " flagged" : "")}>
      <span>{label}</span>
      {children}
    </label>
  );
}

function IdPill({ id }) {
  return id ? (
    <span className="id-pill">YouGile ✓ {String(id).slice(0, 8)}</span>
  ) : (
    <span className="id-pill none">не опознан</span>
  );
}

function Sent({ result }) {
  if (!result) return null;
  return (
    <section className="sent">
      <div className="eyebrow">записано в YouGile</div>
      <h1>Отправлено · {result.written} новых, {result.skipped} уже были</h1>
      <p className="eyebrow">статус созвона: {result.status}</p>
      <ul className="sent-list">
        {(result.written_links || []).map((w, i) => (
          <li key={i}>
            <span className={"badge " + (w.status === "written" ? "written" : "skipped")}>
              {w.status === "written" ? "записано" : "пропущено"}
            </span>
            <span>задача {w.internal_id}</span>
            <span style={{ color: "var(--muted)" }}>
              {w.yougile_task_id ? `YG ${String(w.yougile_task_id).slice(0, 8)}` : ""}
            </span>
          </li>
        ))}
      </ul>
      <p style={{ marginTop: 20 }}>
        <a className="btn btn-ghost" href={result.board_url} target="_blank" rel="noreferrer"
           style={{ textDecoration: "none", display: "inline-block" }}>
          Открыть доску в YouGile →
        </a>
      </p>
    </section>
  );
}
