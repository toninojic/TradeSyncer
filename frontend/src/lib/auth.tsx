import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, onUnauthorized } from "./api";

type AuthState = { status: "loading" } | { status: "anonymous" } | { status: "authenticated"; user: string };

interface AuthContextValue {
  state: AuthState;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  useEffect(() => {
    api
      .get<{ user: string }>("/api/auth/me")
      .then((r) => setState({ status: "authenticated", user: r.user }))
      .catch(() => setState({ status: "anonymous" }));
    return onUnauthorized(() => setState({ status: "anonymous" }));
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const r = await api.post<{ user: string }>("/api/auth/login", { username, password });
    setState({ status: "authenticated", user: r.user });
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post("/api/auth/logout");
    } finally {
      setState({ status: "anonymous" });
    }
  }, []);

  return <AuthContext.Provider value={{ state, login, logout }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
