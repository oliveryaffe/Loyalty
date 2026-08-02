import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  ApiError,
  getMe,
  getToken,
  login as apiLogin,
  MerchantOut,
  setToken as storeToken,
} from "./api/client";

interface AuthContextValue {
  merchant: MerchantOut | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [merchant, setMerchant] = useState<MerchantOut | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const token = getToken();
    if (!token) {
      setIsLoading(false);
      return;
    }
    getMe()
      .then(setMerchant)
      .catch(() => {
        storeToken(null);
        setMerchant(null);
      })
      .finally(() => setIsLoading(false));
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const tok = await apiLogin(email, password);
    storeToken(tok.access_token);
    const me = await getMe();
    setMerchant(me);
  }, []);

  const logout = useCallback(() => {
    storeToken(null);
    setMerchant(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      merchant,
      isLoading,
      isAuthenticated: merchant !== null,
      login,
      logout,
    }),
    [merchant, isLoading, login, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError;
}
