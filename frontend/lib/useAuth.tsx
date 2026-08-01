"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api, auth, type User } from "./api";

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (name: string, email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState>({
  user: null,
  loading: true,
  login: async () => {},
  signup: async () => {},
  logout: () => {},
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // On load, a stored token is a claim, not proof — verify it against /auth/me
  // so an expired or revoked token resolves to "signed out" rather than a broken
  // session that only fails on the first real call.
  useEffect(() => {
    if (!auth.token) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    api
      .me()
      .then((u) => {
        if (!cancelled) setUser(u);
      })
      .catch(() => {
        if (cancelled) return;
        auth.clear();
        setUser(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const result = await api.login(email, password);
    auth.set(result.token);
    setUser(result.user);
  }, []);

  const signup = useCallback(async (name: string, email: string, password: string) => {
    const result = await api.signup(name, email, password);
    auth.set(result.token);
    setUser(result.user);
  }, []);

  const logout = useCallback(() => {
    // Tell the server first, so the token is revoked rather than merely
    // forgotten by this browser. Fire-and-forget on purpose: signing out must
    // never appear to fail or hang, so the local state is cleared regardless of
    // whether the call lands. A revocation that does not reach the server leaves
    // the token expiring on its own schedule, exactly as before.
    void api.logout().catch(() => {});
    auth.clear();
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, login, signup, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
