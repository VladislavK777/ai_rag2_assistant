// SSO Keycloak: authorization code + PKCE (client_id rag2_client).
// Backend — resource server: сюда передаётся только access JWT.
// Refresh — тихий (iframe на Keycloak) или через refresh_token.

const STORAGE = {
  token: "rag2_token",
  refresh: "rag2_refresh",
  expires: "rag2_expires",
  verifier: "rag2_pkce_verifier",
  returnTo: "rag2_return_to",
  idToken: "rag2_id_token",
};

const sso = { issuer: "", clientId: "rag2_client" };

export function getToken(): string | null {
  return localStorage.getItem(STORAGE.token);
}

export function setToken(token: string): void {
  localStorage.setItem(STORAGE.token, token);
}

export function clearToken(): void {
  Object.values(STORAGE).forEach((k) => localStorage.removeItem(k));
}

export function isTokenValid(): boolean {
  const exp = Number(localStorage.getItem(STORAGE.expires) || 0);
  return !!getToken() && Date.now() < exp * 1000 - 30_000; // с запасом 30с
}

// ---------- PKCE helpers ----------

function base64url(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = "";
  bytes.forEach((b) => (bin += String.fromCharCode(b)));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function sha256(input: string): Promise<ArrayBuffer> {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
}

function randomString(len = 64): string {
  const arr = new Uint8Array(len);
  crypto.getRandomValues(arr);
  return base64url(arr.buffer).slice(0, len);
}

// ---------- Инициализация: конфиг SSO от backend ----------

export async function initAuth(): Promise<void> {
  const res = await fetch("/api/v1/auth/config");
  if (res.ok) {
    const cfg = await res.json();
    sso.issuer = cfg.issuer;
    sso.clientId = cfg.client_id;
  }
  // Возврат из Keycloak с code — обмен на токены
  const params = new URLSearchParams(location.search);
  if (params.get("code") && params.get("state")) {
    await exchangeCode(params);
  }
}

// ---------- Логин / logout / refresh ----------

export async function login(): Promise<void> {
  if (!sso.issuer) await initAuth();
  const verifier = randomString();
  const challenge = base64url(await sha256(verifier));
  localStorage.setItem(STORAGE.verifier, verifier);
  localStorage.setItem(STORAGE.returnTo, location.pathname + location.hash);

  const params = new URLSearchParams({
    client_id: sso.clientId,
    response_type: "code",
    scope: "openid",
    redirect_uri: location.origin + "/",
    state: randomString(16),
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  location.href = `${sso.issuer}/protocol/openid-connect/auth?${params}`;
}

export async function exchangeCode(params: URLSearchParams): Promise<void> {
  history.replaceState(null, "", localStorage.getItem(STORAGE.returnTo) || "/");
  const verifier = localStorage.getItem(STORAGE.verifier) || "";
  const body = new URLSearchParams({
    client_id: sso.clientId,
    grant_type: "authorization_code",
    code: params.get("code")!,
    redirect_uri: location.origin + "/",
    code_verifier: verifier,
  });
  const res = await fetch(`${sso.issuer}/protocol/openid-connect/token`, { method: "POST", body });
  if (!res.ok) return;
  const data = await res.json();
  saveTokens(data);
  if (data.id_token) localStorage.setItem(STORAGE.idToken, data.id_token);
}

function saveTokens(data: { access_token: string; refresh_token?: string; expires_in?: number }): void {
  setToken(data.access_token);
  if (data.refresh_token) localStorage.setItem(STORAGE.refresh, data.refresh_token);
  localStorage.setItem(
    STORAGE.expires,
    String(Math.floor(Date.now() / 1000) + (data.expires_in || 300))
  );
}

/** Тихое обновление через refresh_token; false — нужен логин. */
export async function tryRefresh(): Promise<boolean> {
  const refresh = localStorage.getItem(STORAGE.refresh);
  if (!refresh || !sso.issuer) return false;
  const body = new URLSearchParams({
    client_id: sso.clientId,
    grant_type: "refresh_token",
    refresh_token: refresh,
  });
  const res = await fetch(`${sso.issuer}/protocol/openid-connect/token`, { method: "POST", body });
  if (!res.ok) return false;
  saveTokens(await res.json());
  return true;
}

export async function logout(): Promise<void> {
  // Полный выход: сначала сессия Keycloak (требует id_token_hint в KC 26+),
  // затем локальная очистка. Порядок важен: редирект на logout-эндпоинт
  // обязан случиться до reload, иначе SSO-сессия переживёт «выход».
  const idToken = localStorage.getItem(STORAGE.idToken);
  clearToken();
  if (sso.issuer) {
    const params = new URLSearchParams({
      post_logout_redirect_uri: location.origin + "/",
      client_id: sso.clientId, // fallback для KC без валидного id_token_hint
    });
    if (idToken) params.set("id_token_hint", idToken);
    location.href = `${sso.issuer}/protocol/openid-connect/logout?${params}`;
    return;
  }
  location.reload();
}

// ---------- API-клиент с авто-refresh ----------

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  if (!isTokenValid()) {
    const ok = await tryRefresh();
    if (!ok) {
      await login();
      throw new Error("Требуется вход через SSO");
    }
  }
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
    const ok = await tryRefresh();
    if (ok) return api<T>(path, options); // один повтор после refresh
    clearToken();
    await login();
    throw new Error("Сессия истекла");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Ошибка ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// ---------- Остальные модели (без изменений) ----------

export interface Me {
  id: string;
  employee_no: string;
  email: string;
  full_name: string;
  roles: string[];
  department_id: string | null;
}

export interface Department {
  id: string;
  code: string;
  name: string;
  parent_id: string | null;
}

export interface AdminUser {
  id: string;
  employee_no: string;
  email: string;
  full_name: string;
  department_id: string | null;
  is_active: boolean;
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
  if (!isTokenValid()) await tryRefresh();
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

export async function uploadDocument(
  file: File,
  workspaceId: string
): Promise<{ id: string }> {
  if (!isTokenValid()) await tryRefresh();
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
