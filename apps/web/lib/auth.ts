// Lightweight token storage: a cookie (for the middleware route guard) plus a local read (so the API client can add the Bearer header).
// An MVP approach; production can move to an httpOnly cookie plus a route-handler proxy.
const TOKEN_KEY = "sag_token";

export function setToken(token: string) {
  if (typeof document === "undefined") return;
  // 7 days, SameSite=Lax
  document.cookie = `${TOKEN_KEY}=${token}; path=/; max-age=${60 * 60 * 24 * 7}; SameSite=Lax`;
}

export function getToken(): string | null {
  if (typeof document === "undefined") return null;
  const m = document.cookie.match(new RegExp(`(?:^|; )${TOKEN_KEY}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : null;
}

export function clearToken() {
  if (typeof document === "undefined") return;
  document.cookie = `${TOKEN_KEY}=; path=/; max-age=0`;
}

export const TOKEN_COOKIE = TOKEN_KEY;
