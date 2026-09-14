import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import {
  AdminUser,
  Citation,
  ChatStreamEvent,
  Department,
  Me,
  Workspace,
  api,
  chatStream,
  clearToken,
  documentStatus,
  getToken,
  initAuth,
  isTokenValid,
  login,
  logout,
  uploadDocument,
} from "./api";

interface Msg {
  role: "user" | "assistant";
  text: string;
  status?: string;
  citations?: Citation[];
}

const EXAMPLES = [
  "Как оформить отпуск?",
  "Какие этапы в Блоке 1?",
  "Какой бюджет на 2026 год?",
  "Где найти регламент ИТ-сервисов?",
];

function LogoMark({ size = 40 }: { size?: number }) {
  return <div className="logo-mark" style={{ width: size, height: size, fontSize: size / 2.6 }}>R2</div>;
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || "?";
}

// ---------- Логин (SSO Keycloak) ----------

function LoginScreen(props: { onLogin: () => void; error: string; busy: boolean }) {
  return (
    <div id="login-overlay">
      <div className="login-card">
        <div className="login-logo">
          <LogoMark />
          <div>
            <div className="login-title">RAG2 Assistant</div>
          </div>
        </div>
        <div className="login-sub">Корпоративный ассистент по базе знаний</div>
        <div className="login-error">{props.error}</div>
        <div className="login-sub" style={{ marginBottom: 12 }}>
          Вход выполняется через корпоративный SSO (Keycloak)
        </div>
        <button className="btn btn-primary" onClick={props.onLogin} disabled={props.busy}>
          {props.busy ? "Переход…" : "Войти через SSO"}
        </button>
      </div>
    </div>
  );
}

// ---------- Тост ----------

function Toast({ toast }: { toast: { text: string; kind: string } | null }) {
  if (!toast) return null;
  return <div className={`toast ${toast.kind}`}>{toast.text}</div>;
}

// ---------- Модальное окно ----------

function Modal({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) {
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        {children}
      </div>
    </div>
  );
}

// ---------- Админ: пользователи ----------

function UsersTab({ deps, showToast }: { deps: Department[]; showToast: (t: string, kind: string) => void }) {
  const [users, setUsers] = useState<AdminUser[]>([]);

  const load = () => api<AdminUser[]>("/admin/users").then(setUsers).catch((e) => showToast(e.message, "err"));
  useEffect(() => { load(); }, []);

  const depName = (id: string | null) => deps.find((d) => d.id === id)?.name || "—";

  return (
    <>
      <div className="admin-header">
        <h3>Пользователи ({users.length})</h3>
        <span className="row-sub" title="Создание и роли — в Keycloak">управляются в Keycloak</span>
      </div>
      {users.map((u) => (
        <div className="row-item" key={u.id}>
          <div className="row-main">
            <div className="row-title">{u.full_name}</div>
            <div className="row-sub">{u.employee_no} · {u.email} · {depName(u.department_id)}</div>
          </div>
          {!u.is_active && <span className="badge badge-inactive">выкл</span>}
        </div>
      ))}
    </>
  );
}

function UserModal({ user, deps, onClose, onDone, showToast }: {
  user: AdminUser | null;
  deps: Department[];
  onClose: () => void;
  onDone: () => void;
  showToast: (t: string, kind: string) => void;
}) {
  // Пользователи управляются в Keycloak — модалка заменена справочным просмотром.
  return null;
}

// ---------- Админ: департаменты ----------

function DepartmentsTab({ deps, showToast }: { deps: Department[]; showToast: (t: string, kind: string) => void }) {
  const [editing, setEditing] = useState<Department | "new" | null>(null);

  const del = async (d: Department) => {
    if (!confirm(`Удалить департамент «${d.name}»?`)) return;
    try {
      await api(`/admin/departments/${d.id}`, { method: "DELETE" });
      showToast("Департамент удалён", "ok");
      window.dispatchEvent(new Event("deps-changed"));
    } catch (e) { showToast((e as Error).message, "err"); }
  };

  const nameOf = (id: string | null) => deps.find((d) => d.id === id)?.name;

  return (
    <>
      <div className="admin-header">
        <h3>Департаменты ({deps.length})</h3>
        <button className="btn btn-primary btn-sm" onClick={() => setEditing("new")}>+ Добавить</button>
      </div>
      {deps.map((d) => (
        <div className="row-item" key={d.id}>
          <div className="row-main">
            <div className="row-title">{d.name}</div>
            {d.parent_id && <div className="row-sub">внутри «{nameOf(d.parent_id) || "?"}»</div>}
          </div>
          <button className="icon-btn" title="Редактировать" onClick={() => setEditing(d)}>✎</button>
          <button className="icon-btn danger" title="Удалить" onClick={() => del(d)}>✕</button>
        </div>
      ))}
      {editing && (
        <DeptModal
          dept={editing === "new" ? null : editing}
          deps={deps}
          onClose={() => setEditing(null)}
          onDone={() => { setEditing(null); window.dispatchEvent(new Event("deps-changed")); }}
          showToast={showToast}
        />
      )}
    </>
  );
}

function DeptModal({ dept, deps, onClose, onDone, showToast }: {
  dept: Department | null;
  deps: Department[];
  onClose: () => void;
  onDone: () => void;
  showToast: (t: string, kind: string) => void;
}) {
  const [code, setCode] = useState(dept?.code || "");
  const [name, setName] = useState(dept?.name || "");
  const [parentId, setParentId] = useState(dept?.parent_id || "");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      if (dept) {
        await api(`/admin/departments/${dept.id}`, {
          method: "PATCH",
          body: JSON.stringify({ name, parent_id: parentId || null }),
        });
        showToast("Сохранено", "ok");
      } else {
        await api("/admin/departments", {
          method: "POST",
          body: JSON.stringify({ code, name, parent_id: parentId || null }),
        });
        showToast("Департамент создан", "ok");
      }
      onDone();
    } catch (e) {
      showToast((e as Error).message, "err");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={dept ? "Редактировать департамент" : "Новый департамент"} onClose={onClose}>
      {!dept && (
        <>
          <label className="label">Код (формат ДЕП-хххх, из LDAP)</label>
          <input className="field" value={code} onChange={(e) => setCode(e.target.value.toUpperCase())} placeholder="ДЕП-0600" />
        </>
      )}
      <label className="label">Название</label>
      <input className="field" value={name} onChange={(e) => setName(e.target.value)} placeholder="Маркетинг" />
      <label className="label">Родительский департамент</label>
      <select className="field" value={parentId} onChange={(e) => setParentId(e.target.value)}>
        <option value="">— корневой —</option>
        {deps.filter((d) => d.id !== dept?.id).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
      </select>
      <div className="modal-actions">
        <button className="btn btn-ghost" onClick={onClose}>Отмена</button>
        <button className="btn btn-primary" onClick={save} disabled={busy || !name || (!dept && !/^ДЕП-\d{4}$/.test(code))}>
          {busy ? "…" : dept ? "Сохранить" : "Создать"}
        </button>
      </div>
    </Modal>
  );
}

// ---------- Админ-панель ----------

function AdminPanel({ showToast }: { showToast: (t: string, kind: string) => void }) {
  const [tab, setTab] = useState<"users" | "deps">("users");
  const [deps, setDeps] = useState<Department[]>([]);

  const loadDeps = () => api<Department[]>("/admin/departments").then(setDeps).catch(() => {});
  useEffect(() => {
    loadDeps();
    const h = () => loadDeps();
    window.addEventListener("deps-changed", h);
    return () => window.removeEventListener("deps-changed", h);
  }, []);

  return (
    <aside className="admin-panel">
      <div className="admin-tabs">
        <button className={`admin-tab ${tab === "users" ? "active" : ""}`} onClick={() => setTab("users")}>Пользователи</button>
        <button className={`admin-tab ${tab === "deps" ? "active" : ""}`} onClick={() => setTab("deps")}>Департаменты</button>
      </div>
      <div className="admin-body">
        {tab === "users" ? <UsersTab deps={deps} showToast={showToast} /> : <DepartmentsTab deps={deps} showToast={showToast} />}
      </div>
    </aside>
  );
}

// ---------- Приложение ----------

function App() {
  const [authed, setAuthed] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [workspaceId, setWorkspaceId] = useState("");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [uploadStatus, setUploadStatus] = useState("");
  const [toast, setToast] = useState<{ text: string; kind: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const chatRef = useRef<HTMLDivElement>(null);
  const toastTimer = useRef<number>(0);

  const showToast = (text: string, kind: string) => {
    setToast({ text, kind });
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3200);
  };

  // SSO-инициализация: обработка code из Keycloak + проверка текущего токена
  useEffect(() => {
    initAuth().finally(() => setAuthReady(true));
  }, []);

  useEffect(() => {
    if (!authReady) return;
    setAuthed(isTokenValid());
  }, [authReady]);

  useEffect(() => {
    if (!authed) return;
    api<Me>("/auth/me")
      .then(setMe)
      .catch(() => {});
    api<Workspace[]>("/admin/workspaces")
      .then((ws) => {
        setWorkspaces(ws);
        if (ws.length && !workspaceId) setWorkspaceId(ws[0].id);
      })
      .catch(() => {});
  }, [authed]);

  useEffect(() => {
    chatRef.current?.scrollTo(0, chatRef.current.scrollHeight);
  }, [messages]);

  const handleLogin = () => {
    // редирект на Keycloak (PKCE), возврат — в initAuth
    login();
  };

  const handleLogout = () => {
    logout();
  };

  const handleUpload = async (file: File) => {
    if (!workspaceId) {
      showToast("Нет доступного пространства", "err");
      return;
    }
    setUploadStatus(`Индексация «${file.name}»…`);
    try {
      const { id } = await uploadDocument(file, workspaceId);
      // Индексация асинхронная: поллим статус, чтобы не показывать
      // зелёный «загружен» для документа, который упал при индексации
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const st = await documentStatus(id);
        if (st.status === "indexed") {
          setUploadStatus(`«${file.name}» проиндексирован.`);
          showToast("Документ проиндексирован", "ok");
          return;
        }
        if (st.status === "failed") {
          setUploadStatus("");
          showToast(
            `Индексация «${file.name}» не удалась: документ не дал ни одного чанка (текст слишком короткий или пустой)`,
            "err"
          );
          return;
        }
      }
      setUploadStatus(`«${file.name}» ещё индексируется…`);
    } catch (e) {
      setUploadStatus("");
      showToast((e as Error).message, "err");
    }
  };

  const handleSend = async (question?: string) => {
    const q = (question ?? input).trim();
    if (!q || busy) return;
    if (!workspaceId) {
      showToast("Нет доступного пространства", "err");
      return;
    }
    setInput("");
    setBusy(true);
    setMessages((m) => [
      ...m,
      { role: "user", text: q },
      { role: "assistant", text: "", status: "Обработка…" },
    ]);

    const onEvent = (e: ChatStreamEvent) => {
      setMessages((msgs) => {
        const copy = [...msgs];
        const last = { ...copy[copy.length - 1] };
        if (e.type === "status") {
          last.status = e.data as string;
        } else if (e.type === "answer") {
          last.text = e.data as string;
          last.status = undefined;
        } else if (e.type === "citations") {
          last.citations = e.data as Citation[];
        } else if (e.type === "error") {
          last.text = `⚠ ${e.data}`;
          last.status = undefined;
        }
        copy[copy.length - 1] = last;
        return copy;
      });
    };

    try {
      await chatStream(q, workspaceId, sessionId, onEvent);
    } catch {
      onEvent({ type: "error", data: "Поток прерван" });
    } finally {
      setBusy(false);
    }
  };

  if (!authReady) return null;
  if (!authed) {
    return (
      <LoginScreen onLogin={handleLogin} error="" busy={false} />
    );
  }

  const isAdmin = !!me?.roles.includes("admin");

  return (
    <>
      <header>
        <span className="logo"><LogoMark size={30} /> RAG2</span>
        <select
          className="ws-select"
          value={workspaceId}
          onChange={(e) => setWorkspaceId(e.target.value)}
          title="Рабочее пространство"
        >
          {workspaces.map((w) => (
            <option key={w.id} value={w.id}>{w.name}</option>
          ))}
        </select>
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,.docx,.txt,.md"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) handleUpload(f);
            e.target.value = "";
          }}
        />
        <button className="btn btn-ghost btn-sm" onClick={() => fileRef.current?.click()} title="Загрузить документ в базу знаний">
          📄 Загрузить документ
        </button>
        {uploadStatus && <span className="upload-status">{uploadStatus}</span>}
        <div className="header-spacer" />
        <div className="user-chip">
          <div>
            <div className="user-name">{me?.full_name || "…"}</div>
            <div className="user-role">{isAdmin ? "администратор" : "сотрудник"}</div>
          </div>
          <div className="avatar">{initials(me?.full_name || "?")}</div>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={handleLogout}>Выйти</button>
      </header>

      <div className="layout">
        <div className="chat-col">
          <main id="chat" ref={chatRef}>
            <div className="chat-inner">
              {messages.length === 0 && (
                <div className="empty-state">
                  <div className="empty-icon">✨</div>
                  <div className="empty-title">Чем помочь?</div>
                  <div className="empty-hint">
                    Задайте вопрос — ассистент найдёт ответ в документах
                    этого пространства и укажет источники.
                  </div>
                  <div className="empty-examples">
                    {EXAMPLES.map((ex) => (
                      <button key={ex} className="example-chip" onClick={() => handleSend(ex)}>{ex}</button>
                    ))}
                  </div>
                </div>
              )}
              {messages.map((m, i) => (
                <div key={i} className={`msg ${m.role}`}>
                  {m.role === "assistant" && <div className="msg-avatar">R2</div>}
                  <div className="bubble">
                    {m.status && <div className="status"><span className="status-dot" />{m.status}</div>}
                    {m.role === "assistant" && m.text
                      ? <div className="md"><ReactMarkdown>{m.text}</ReactMarkdown></div>
                      : m.text}
                    {m.citations && m.citations.length > 0 && (
                      <div className="citations">
                        <div className="citations-title">Источники</div>
                        {m.citations.map((c, j) => (
                          <span key={j} className="citation">
                            {c.title}{c.page ? `, стр. ${c.page}` : ""}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </main>

          <footer>
            <div id="inputbar">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSend()}
                placeholder="Задайте вопрос по документам…"
                disabled={busy}
              />
              <button id="send" className="btn btn-primary" onClick={() => handleSend()} disabled={busy}>
                {busy ? "…" : "Отправить"}
              </button>
            </div>
            <div className="input-note">Ответы основаны на документах рабочего пространства</div>
          </footer>
        </div>

        {isAdmin && <AdminPanel showToast={showToast} />}
      </div>

      <Toast toast={toast} />
    </>
  );
}

const rootEl = document.getElementById("root");
createRoot(rootEl || document.body).render(<App />);
