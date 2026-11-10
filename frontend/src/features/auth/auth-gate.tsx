import { useQuery, useQueryClient } from "@tanstack/react-query";
import { LogOut } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Brand } from "@/components/layout/brand";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, api, type AuthStatus } from "@/lib/api";
import { UNAUTHORIZED_EVENT, authEvents, authToken } from "@/lib/auth";

const AUTH_KEY = ["auth", "status"] as const;

export function useAuth() {
  const client = useQueryClient();
  const status = useQuery({
    queryKey: AUTH_KEY,
    queryFn: ({ signal }) => api.authStatus(signal),
    retry: false,
    staleTime: 60_000,
  });
  return {
    /** True only when the server says sign-in is required. An unreachable/older API is not "required". */
    required: status.data?.required ?? false,
    authenticated: status.data?.authenticated ?? true,
    refresh: () => client.invalidateQueries({ queryKey: AUTH_KEY }),
    signOut: () => {
      authToken.clear();
      void client.invalidateQueries({ queryKey: AUTH_KEY });
      client.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
    },
  };
}

/** Shows the sign-in form instead of the app when the API requires it and this browser is signed out. */
export function AuthGate({ children }: { children: ReactNode }) {
  const auth = useAuth();

  // Any 401 from the API (expired/invalid token) means: signed out. Set that directly instead of
  // re-fetching, so it cannot be lost to a status request that is still in flight.
  const client = useQueryClient();
  useEffect(() => {
    const onUnauthorized = () =>
      client.setQueryData<AuthStatus>(AUTH_KEY, { required: true, authenticated: false });
    authEvents.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => authEvents.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [client]);

  if (auth.required && !auth.authenticated) return <LoginPage onSignedIn={() => void auth.refresh()} />;
  return <>{children}</>;
}

export function SignOutButton() {
  const auth = useAuth();
  if (!auth.required || !auth.authenticated) return null;
  return (
    <Button variant="ghost" size="sm" onClick={auth.signOut} data-testid="sign-out">
      <LogOut /> Sign out
    </Button>
  );
}

function LoginPage({ onSignedIn }: { onSignedIn: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(password);
      setPassword("");
      onSignedIn();
    } catch (e) {
      if (e instanceof ApiError && e.code === "too_many_attempts") {
        setError("Too many failed attempts. Wait a few minutes and try again.");
      } else if (e instanceof ApiError && e.kind === "http") {
        setError("That password is not correct.");
      } else {
        setError("Could not reach the server. Check that the API is running.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="space-y-3">
          <Brand />
          <CardTitle>Sign in</CardTitle>
          <CardDescription>This dashboard controls live video processing and is password protected.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="space-y-4" aria-label="Sign in">
            <div className="space-y-2">
              <label htmlFor="password" className="text-sm font-medium">
                Password
              </label>
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                autoFocus
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                aria-invalid={error !== null}
                aria-describedby={error ? "login-error" : undefined}
                className="h-10 w-full rounded-md border bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              />
            </div>
            {error && (
              <p id="login-error" role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
            <Button type="submit" className="w-full" disabled={busy || password.length === 0}>
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
