const TOKEN_KEY = "rag2_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getToken();
  const res = await fetch(`/api/v1${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });
  if (res.status === 401) {
    clearToken();
    location.reload();
    throw new Error("Не авторизован");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Ошибка ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export interface Me {
  id: string;
  email: string;
  full_name: string;
  roles: string[];
  department_id: string | null;
}

export interface Department {
  id: string;
  name: string;
  parent_id: string | null;
}

export interface AdminUser {
  id: string;
  email: string;
  full_name: string;
  department_id: string | null;
  is_active: boolean;
  is_admin: boolean;
}

export interface Workspace {
  id: string;
  name: string;
}

export interface Citation {
  document_id: string;
  title: string;
  page: number | null;
}

export interface ChatStreamEvent {
  type: "status" | "answer" | "citations" | "done" | "error";
  data: string | Citation[];
}

/** SSE-подобное чтение потока чата (POST + ReadableStream). */
export async function chatStream(
  message: string,
  workspaceId: string,
  sessionId: string | null,
  onEvent: (e: ChatStreamEvent) => void
): Promise<void> {
  const token = getToken();
  const res = await fetch("/api/v1/chat/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      message,
      session_id: sessionId,
      workspace_id: workspaceId,
    }),
  });

  if (!res.ok || !res.body) {
    onEvent({ type: "error", data: "Ошибка соединения с сервером" });
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (line.startsWith("data: ")) {
        try {
          onEvent(JSON.parse(line.slice(6)) as ChatStreamEvent);
        } catch {
          // пропуск некорректного события
        }
      }
    }
  }
}

export async function login(email: string, password: string): Promise<void> {
  const res = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "Ошибка входа");
  }
  const data = await res.json();
  setToken(data.access_token);
}

export async function uploadDocument(
  file: File,
  workspaceId: string
): Promise<{ id: string }> {
  const token = getToken();
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`/api/v1/ingest/upload?workspace_id=${workspaceId}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: form,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Ошибка ${res.status}`);
  }
  return res.json();
}

export async function documentStatus(
  documentId: string
): Promise<{ id: string; title: string; status: string }> {
  const res = await fetch(`/api/v1/ingest/status/${documentId}`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Ошибка ${res.status}`);
  }
  return res.json();
}