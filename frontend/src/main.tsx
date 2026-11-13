import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { ToastProvider } from "@/components/ui";
import { AuthProvider, useAuth } from "@/lib/auth";
import { LiveProvider } from "@/lib/live";
import { AccountsPage } from "@/pages/Accounts";
import { CopySettingsPage } from "@/pages/CopySettings";
import { DashboardPage } from "@/pages/Dashboard";
import { LoginPage } from "@/pages/Login";
import { RiskPage } from "@/pages/Risk";
import { SimulatorPage } from "@/pages/Simulator";
import { SystemLogPage } from "@/pages/SystemLog";
import { TradeLogPage } from "@/pages/TradeLog";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 2000 } },
});

function App() {
  const { state } = useAuth();
  if (state.status === "loading") {
    return <div className="flex min-h-screen items-center justify-center text-slate-500">Loading…</div>;
  }
  if (state.status === "anonymous") return <LoginPage />;
  return (
    <LiveProvider>
      <BrowserRouter>
        <Layout>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/accounts" element={<AccountsPage />} />
            <Route path="/copy-settings" element={<CopySettingsPage />} />
            <Route path="/risk" element={<RiskPage />} />
            <Route path="/trades" element={<TradeLogPage />} />
            <Route path="/logs" element={<SystemLogPage />} />
            <Route path="/simulator" element={<SimulatorPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Layout>
      </BrowserRouter>
    </LiveProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <AuthProvider>
          <App />
        </AuthProvider>
      </ToastProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
