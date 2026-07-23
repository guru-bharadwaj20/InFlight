"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/useAuth";
import { Sidebar } from "./Sidebar";
import { Logo } from "./Logo";

const AUTH_ROUTES = new Set(["/login", "/signup"]);

/**
 * Decides the frame: the login and signup pages render bare, everything else
 * renders the sidebar and is gated behind a signed-in user. Redirects happen in
 * an effect (never during render) so the router is not mutated mid-paint.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const isAuthRoute = AUTH_ROUTES.has(pathname);

  useEffect(() => {
    if (loading) return;
    if (!user && !isAuthRoute) router.replace("/login");
    if (user && isAuthRoute) router.replace("/");
  }, [user, loading, isAuthRoute, router]);

  // A brief splash while the stored token is being verified, so the login page
  // does not flash before a valid session resolves.
  if (loading) return <Splash />;

  if (isAuthRoute) {
    return <div className="h-screen overflow-y-auto">{children}</div>;
  }

  if (!user) return <Splash />; // redirect is in flight

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="relative min-w-0 flex-1 overflow-hidden">{children}</main>
    </div>
  );
}

function Splash() {
  return (
    <div className="flex h-screen items-center justify-center">
      <div className="animate-pulse">
        <Logo className="h-10 w-10" id="splash" />
      </div>
    </div>
  );
}
