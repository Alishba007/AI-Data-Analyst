import axios from "axios";

// In dev, Vite's proxy (vite.config.js) forwards /api/* to localhost:8000.
// In production, set VITE_API_URL to the deployed backend's real URL.
const API_BASE = import.meta.env.VITE_API_URL || "/api";

const TOKEN_KEY = "ai-data-analyst-token";

// localStorage (not sessionStorage) is deliberate here: logging in should
// persist across tab closes and browser restarts, same as any normal app
// login — only an explicit logout (or token expiry, 1 week) should clear it.
export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

const client = axios.create({ baseURL: API_BASE });

// Attach the JWT to every request automatically, so callers never have to
// think about it. Requests made before login simply go out without the
// header — the backend correctly rejects those with 401.
client.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// If the token is invalid or expired, the backend returns 401 on a
// protected route — treat that as "logged out" everywhere in the app
// rather than making each caller handle it individually.
client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401 && !error.config.url.includes("/auth/")) {
      clearToken();
      window.location.reload();
    }
    return Promise.reject(error);
  }
);

export async function signup(email, password) {
  const { data } = await client.post("/auth/signup", { email, password });
  setToken(data.token);
  return data;
}

export async function login(email, password) {
  const { data } = await client.post("/auth/login", { email, password });
  setToken(data.token);
  return data;
}

export async function checkHealth() {
  const { data } = await client.get("/health");
  return data;
}

export async function uploadFile(file) {
  const formData = new FormData();
  formData.append("file", file);
  const { data } = await client.post("/upload", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export async function askQuestion(question) {
  const { data } = await client.post("/ask", { question });
  return data;
}

export async function getInsights(tableName) {
  const { data } = await client.post("/insights", { table_name: tableName });
  return data;
}