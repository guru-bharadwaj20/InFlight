"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useAuth } from "@/lib/useAuth";
import { Logo } from "./Logo";
import { ThemeToggle } from "./theme";

/** Login and signup are the same form minus a field, so they share one shell. */
export function AuthForm({ mode }: { mode: "login" | "signup" }) {
  const router = useRouter();
  const { login, signup } = useAuth();
  const isSignup = mode === "signup";

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (isSignup) await signup(name.trim(), email.trim(), password);
      else await login(email.trim(), password);
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center px-6">
      <div className="hero-wash" />
      <ThemeToggle className="absolute right-4 top-4" />

      <div className="relative w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center text-center">
          <Logo className="h-11 w-11" id="auth" />
          <h1 className="mt-3 font-serif text-3xl tracking-tight">
            In<span className="text-flight">Flight</span>
          </h1>
          <p className="mt-1 text-sm text-ink-soft">
            {isSignup ? "Create your account" : "Welcome back"}
          </p>
        </div>

        <form
          onSubmit={submit}
          className="space-y-3 rounded-2xl border border-edge bg-surface p-6 shadow-composer"
        >
          {isSignup && (
            <Field
              label="Name"
              value={name}
              onChange={setName}
              type="text"
              autoComplete="name"
              placeholder="Ada Lovelace"
              required
            />
          )}
          <Field
            label="Email"
            value={email}
            onChange={setEmail}
            type="email"
            autoComplete="email"
            placeholder="you@example.com"
            required
          />
          <Field
            label="Password"
            value={password}
            onChange={setPassword}
            type="password"
            autoComplete={isSignup ? "new-password" : "current-password"}
            placeholder={isSignup ? "at least 8 characters" : "••••••••"}
            required
            minLength={isSignup ? 8 : undefined}
          />

          {error && (
            <p className="rounded-lg border border-failed/30 bg-failed/5 px-3 py-2 text-sm text-failed">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="w-full rounded-xl bg-flight py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {busy ? "One moment…" : isSignup ? "Create account" : "Sign in"}
          </button>
        </form>

        <p className="mt-4 text-center text-sm text-ink-soft">
          {isSignup ? "Already have an account? " : "New here? "}
          <Link
            href={isSignup ? "/login" : "/signup"}
            className="font-medium text-flight hover:underline"
          >
            {isSignup ? "Sign in" : "Create one"}
          </Link>
        </p>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  ...props
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange" | "value">) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-ink-soft">{label}</span>
      <input
        {...props}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-sm outline-none transition-colors placeholder:text-ink-faint focus:border-flight"
      />
    </label>
  );
}
