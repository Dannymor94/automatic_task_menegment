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

// Сегодняшняя дата в формате YYYY-MM-DD по локальному времени (для кнопки «Сегодня»).
function todayISO() {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

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

  const openReview = (meeting_id) => {
    setMeetingId(meeting_id);
    setStatus("uploaded");
    setScreen("processing");
  };

  // Вернуться в главное меню (шаг 1). Сбрасываем состояние текущего разбора.
  const goHome = () => {
    setScreen("upload");
    setMeetingId(null);
    setStatus(null);
    setTasks([]);
    setSent(null);
    setError(null);
  };

  const startUpload = async (source, project) => {
    setError(null);
    try {
      const { meeting_id } = source.url
        ? await api.uploadUrl(source.url, project)
        : await api.upload(source.file, project);
      openReview(meeting_id);
    } catch (e) {
      setError(String(e.message || e));
    }
  };

  // SimpleNote: обработать выбранные заметки → открыть ревью первой (остальные тоже в БД).
  const startSimplenote = async (noteIds, project) => {
    setError(null);
    try {
      const { results } = await api.simplenoteProcess(noteIds, project);
      const ok = results.filter((r) => r.meeting_id);
      if (ok.length === 0) throw new Error("Ни одна заметка не обработалась");
      openReview(ok[0].meeting_id);
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
      <Masthead meetingId={meetingId} status={status} onHome={goHome} canHome={screen !== "upload"} />
      <main className="wrap">
        {error && <p className="err">⚠ {error}</p>}
        {screen === "upload" && <Upload onStart={startUpload} onSimplenote={startSimplenote} />}
        {screen === "processing" && <Processing status={status} onHome={goHome} />}
        {screen === "review" && (
          <Review tasks={tasks} setTasks={setTasks} onSend={send} onHome={goHome} />
        )}
        {screen === "sent" && <Sent result={sent} onHome={goHome} />}
      </main>
    </>
  );
}

function Masthead({ meetingId, status, onHome, canHome }) {
  return (
    <header className="masthead">
      <div className="masthead-inner">
        <div
          className={"wordmark" + (canHome ? " clickable" : "")}
          onClick={canHome ? onHome : undefined}
          role={canHome ? "button" : undefined}
          title={canHome ? "В главное меню" : undefined}
        >
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

const SOURCES = [
  { key: "audio", label: "Аудио / транскрипт" },
  { key: "url", label: "Ссылка" },
  { key: "simplenote", label: "SimpleNote" },
];

function Upload({ onStart, onSimplenote }) {
  const [source, setSource] = useState("audio");
  const [file, setFile] = useState(null);
  const [url, setUrl] = useState("");
  const [projects, setProjects] = useState([]); // [{project_id, title, board_id, column_id}]
  const [projectTitle, setProjectTitle] = useState("");
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [over, setOver] = useState(false);
  const [teamOpen, setTeamOpen] = useState(false);
  const inputRef = useRef();

  useEffect(() => {
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

      <div className="source-tabs">
        {SOURCES.map((s) => (
          <button
            key={s.key}
            className={"source-tab" + (source === s.key ? " active" : "")}
            onClick={() => setSource(s.key)}
          >
            {s.label}
          </button>
        ))}
      </div>

      {source === "audio" && (
        <>
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
          {/* input — СИБЛИНГ дропзоны, не потомок (иначе input.click() всплывает обратно). */}
          <input
            ref={inputRef}
            type="file"
            accept=".mp3,.mp4,.m4a,.wav,.webm,.txt,.md"
            style={{ display: "none" }}
            onChange={(e) => { setFile(e.target.files[0] || null); setUrl(""); }}
          />
        </>
      )}

      {source === "url" && (
        <div className="url-row">
          <span className="eyebrow">ссылка на файл</span>
          <input
            className="text-input url-input"
            placeholder="https://… · Яндекс.Диск · Google Drive"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
        </div>
      )}

      {/* Проект — общий для всех источников */}
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
        {source === "audio" && (
          <button className="btn" disabled={!file} onClick={() => onStart({ file }, selectedProject)}>
            Разобрать созвон
          </button>
        )}
        {source === "url" && (
          <button className="btn" disabled={!url.trim()} onClick={() => onStart({ url: url.trim() }, selectedProject)}>
            Скачать и разобрать
          </button>
        )}
      </div>

      {source === "simplenote" && (
        <SimpleNotePanel project={selectedProject} onProcess={onSimplenote} />
      )}

      {teamOpen && <TeamModal projects={projects} defaultProject={projectTitle} onClose={() => setTeamOpen(false)} />}
    </section>
  );
}

function SimpleNotePanel({ project, onProcess }) {
  const [tag, setTag] = useState("task");
  const [notes, setNotes] = useState(null); // null = ещё не грузили
  const [checked, setChecked] = useState({}); // id -> bool
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const load = async () => {
    setLoading(true); setError(null); setNotes(null); setChecked({});
    try {
      setNotes(await api.simplenoteNotes(tag.trim() || null));
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  };

  const selectedIds = Object.keys(checked).filter((id) => checked[id]);

  return (
    <div className="sn-panel">
      <div className="sn-controls">
        <div className="field">
          <label htmlFor="sn-tag">Тег</label>
          <input id="sn-tag" className="text-input" value={tag}
            onChange={(e) => setTag(e.target.value)} placeholder="task" />
        </div>
        <button className="btn btn-ghost" onClick={load} disabled={loading}>
          {loading ? "Загрузка…" : "Загрузить заметки"}
        </button>
      </div>

      {error && <p className="err">⚠ {error}</p>}

      {notes && notes.length === 0 && (
        <p className="eyebrow" style={{ marginTop: 14 }}>Заметок с тегом «{tag}» не найдено.</p>
      )}

      {notes && notes.length > 0 && (
        <>
          <ul className="sn-list">
            {notes.map((n) => (
              <li key={n.id}>
                <input type="checkbox" checked={!!checked[n.id]}
                  onChange={(e) => setChecked({ ...checked, [n.id]: e.target.checked })} />
                <span className="sn-preview">{n.preview || "(пустая заметка)"}</span>
                <span className={"sn-badge " + (n.type === "structured" ? "struct" : "raw")}>
                  {n.type === "structured" ? "структура" : "сырьё"}
                </span>
              </li>
            ))}
          </ul>
          <div className="field-row">
            <span className="count">выбрано <b>{selectedIds.length}</b> из {notes.length}</span>
            <button className="btn" disabled={selectedIds.length === 0}
              onClick={() => onProcess(selectedIds, project)}>
              Обработать выбранные
            </button>
          </div>
        </>
      )}
    </div>
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

  // Отметить/снять дефолтную роль (исполнитель/контролёр) для проекта человека.
  const setDefault = async (p, role) => {
    setWarning(null);
    try {
      await api.setDefaultRole(p.id, p.default_role === role ? null : role);
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
        <p className="def-hint">★ Отметьте дефолтных <b>И</b> — исполнителя и <b>К</b> — контролёра проекта.
          Они подставятся в задачи, где роль не заполнена. Дефолт — один на проект.</p>

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
                <span className="t-default">
                  <button className={"def-btn" + (p.default_role === "assignee" ? " on" : "")}
                    title="Дефолтный исполнитель проекта"
                    onClick={() => setDefault(p, "assignee")}>И</button>
                  <button className={"def-btn" + (p.default_role === "controller" ? " on" : "")}
                    title="Дефолтный контролёр проекта"
                    onClick={() => setDefault(p, "controller")}>К</button>
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

function Processing({ status, onHome }) {
  const idx = STEPS.findIndex((s) => s.key === status);
  const active = idx === -1 ? 0 : idx;
  return (
    <section className="proc">
      <button className="btn-ghost btn-small back-btn" onClick={onHome}>← В главное меню</button>
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

// Дефолт роли для проекта: точное совпадение проекта; если у задачи нет проекта
// или в проекте нет дефолта — берём единственный отмеченный дефолт, если он один.
function defaultName(people, role, project) {
  const marked = people.filter((p) => p.default_role === role);
  if (project) {
    const inProj = marked.filter((p) => p.project === project);
    if (inProj.length) return inProj[0].name;
  }
  return marked.length === 1 ? marked[0].name : null;
}

function Review({ tasks, setTasks, onSend, onHome }) {
  const [people, setPeople] = useState([]);
  const [projects, setProjects] = useState([]);
  const [teamOpen, setTeamOpen] = useState(false);
  const prefilled = useRef(false);

  const loadPeople = () => api.people().then(setPeople).catch(() => {});
  useEffect(() => {
    loadPeople();
    api.yougileProjects().then(setProjects).catch(() => {});
  }, []);

  // Один раз после загрузки людей: где исполнитель/контролёр пусты — подставить дефолт проекта.
  useEffect(() => {
    if (prefilled.current || people.length === 0 || tasks.length === 0) return;
    prefilled.current = true;
    setTasks(tasks.map((t) => ({
      ...t,
      assignee: t.assignee || defaultName(people, "assignee", t.project) || t.assignee,
      controller: t.controller || defaultName(people, "controller", t.project) || t.controller,
    })));
  }, [people, tasks, setTasks]);

  const flaggedCount = tasks.filter((t) => (t.validator_flags || []).length).length;
  const approvedCount = tasks.filter((t) => t._approved).length;

  const update = (i, patch) =>
    setTasks(tasks.map((t, j) => (j === i ? { ...t, ...patch } : t)));

  // Имена для подсказок комбобокса (по проекту задачи, иначе все).
  const namesFor = (project) =>
    people.filter((p) => !project || !p.project || p.project === project).map((p) => p.name);

  return (
    <section>
      <div className="review-head">
        <div>
          <button className="btn-ghost btn-small back-btn" onClick={onHome}>← В главное меню</button>
          <div className="eyebrow">шаг 3 · ревью · запись только по одобрению</div>
          <h1>Черновики задач</h1>
        </div>
        <div className="review-head-right">
          <button className="btn-ghost btn-small" onClick={() => setTeamOpen(true)}>
            ✎ Роли по умолчанию
          </button>
          <div className="tally">
            {tasks.length} задач · <b>{flaggedCount}</b> с флагами
          </div>
        </div>
      </div>

      {tasks.map((t, i) => (
        <TaskCard key={t.internal_id || i} task={t} names={namesFor(t.project)}
          onChange={(p) => update(i, p)} />
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

      {teamOpen && (
        <TeamModal projects={projects} defaultProject={tasks[0]?.project || ""}
          onClose={() => { setTeamOpen(false); loadPeople(); }} />
      )}
    </section>
  );
}

function TaskCard({ task, names = [], onChange }) {
  const [showSrc, setShowSrc] = useState(false);
  const flags = task.validator_flags || [];
  const hasFlag = (f) => flags.includes(f);
  const fieldFlagged = (field) => hasFlag(FIELD_FLAG[field]);
  const noGrounding = hasFlag("no_grounding");
  const uid = task.internal_id || task.title || "x";
  const listA = `who-a-${uid}`;
  const listC = `who-c-${uid}`;

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
          <input list={listA} placeholder="— задать роль —"
            value={task.assignee || ""}
            onChange={(e) => onChange({ assignee: e.target.value || null })} />
          <datalist id={listA}>
            {names.map((n) => <option key={n} value={n} />)}
          </datalist>
          <IdPill id={task.assignee_id} />
        </Field>
        <Field label="Контролёр" flagged={fieldFlagged("controller")}>
          <input list={listC} placeholder="— задать роль —"
            value={task.controller || ""}
            onChange={(e) => onChange({ controller: e.target.value || null })} />
          <datalist id={listC}>
            {names.map((n) => <option key={n} value={n} />)}
          </datalist>
          <IdPill id={task.controller_id} />
        </Field>
        <Field label="Срок" flagged={fieldFlagged("due_date")}>
          <div className="due-row">
            <input type="date" value={task.due_date || ""}
              onChange={(e) => onChange({ due_date: e.target.value || null })} />
            <button type="button" className="today-btn" title="Поставить сегодняшнюю дату"
              onClick={() => onChange({ due_date: todayISO() })}>Сегодня</button>
          </div>
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

function Sent({ result, onHome }) {
  if (!result) return null;
  return (
    <section className="sent">
      <button className="btn-ghost btn-small back-btn" onClick={onHome}>← В главное меню</button>
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
