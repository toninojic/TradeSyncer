// Live data over the WebSocket: dashboard snapshot, trade-log and system-log events.

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "./api";
import { useAuth } from "./auth";
import type { FollowerOrder, LogEntry, Snapshot, WsMessage } from "./types";

type Listener = (message: WsMessage) => void;

interface LiveContextValue {
  snapshot: Snapshot | null;
  connected: boolean;
  recentAlerts: LogEntry[];
  /** recent copy latencies (ms) per follower id, newest last */
  latencies: Record<number, number[]>;
  subscribe: (listener: Listener) => () => void;
  refresh: () => Promise<void>;
}

const LiveContext = createContext<LiveContextValue | null>(null);
const LATENCY_SAMPLES = 30;
const COPY_ACTIONS = new Set(["copy_fill", "mirror_entry", "mirror_exit", "modify", "cancel"]);

export function LiveProvider({ children }: { children: ReactNode }) {
  const { logout } = useAuth();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [recentAlerts, setRecentAlerts] = useState<LogEntry[]>([]);
  const [latencies, setLatencies] = useState<Record<number, number[]>>({});
  const listeners = useRef(new Set<Listener>());
  const seenLatency = useRef(new Set<string>());

  const refresh = useCallback(async () => {
    try {
      setSnapshot(await api.get<Snapshot>("/api/status"));
    } catch {
      /* the auth layer handles 401 */
    }
  }, []);

  const recordLatency = useCallback((order: FollowerOrder) => {
    if (order.latency_ms === null || !COPY_ACTIONS.has(order.action) || !order.leader_event_id) return;
    if (seenLatency.current.has(order.id)) return;
    seenLatency.current.add(order.id);
    setLatencies((prev) => {
      const list = [...(prev[order.follower_id] ?? []), order.latency_ms as number].slice(-LATENCY_SAMPLES);
      return { ...prev, [order.follower_id]: list };
    });
  }, []);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry = 0;
    let stopped = false;
    let timer: number | undefined;
    let ping: number | undefined;

    const connect = () => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/ws`);
      socket.onopen = () => {
        retry = 0;
        setConnected(true);
        ping = window.setInterval(() => socket?.readyState === WebSocket.OPEN && socket.send("ping"), 20000);
      };
      socket.onmessage = (event) => {
        const message = JSON.parse(event.data) as WsMessage;
        if (message.type === "snapshot") setSnapshot(message.data);
        if (message.type === "log" && message.data.level !== "info") {
          setRecentAlerts((prev) => [message.data, ...prev].slice(0, 50));
        }
        if (message.type === "follower_order") recordLatency(message.data);
        listeners.current.forEach((listener) => listener(message));
      };
      socket.onclose = (event) => {
        setConnected(false);
        window.clearInterval(ping);
        if (stopped) return;
        if (event.code === 4401) {
          void logout();
          return;
        }
        retry += 1;
        timer = window.setTimeout(connect, Math.min(10000, 500 * 2 ** retry));
      };
    };

    void refresh();
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      window.clearInterval(ping);
      socket?.close();
    };
  }, [logout, recordLatency, refresh]);

  // Seed the alert list from the stored system log.
  useEffect(() => {
    api
      .get<LogEntry[]>("/api/logs?level=warning,error&limit=20")
      .then((entries) => setRecentAlerts((prev) => (prev.length ? prev : entries)))
      .catch(() => undefined);
  }, []);

  const subscribe = useCallback((listener: Listener) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  return (
    <LiveContext.Provider value={{ snapshot, connected, recentAlerts, latencies, subscribe, refresh }}>
      {children}
    </LiveContext.Provider>
  );
}

export function useLive(): LiveContextValue {
  const ctx = useContext(LiveContext);
  if (!ctx) throw new Error("useLive outside LiveProvider");
  return ctx;
}

/** Run `handler` for live messages of the given types (debounced). */
export function useLiveEvents(types: WsMessage["type"][], handler: () => void, delayMs = 300): void {
  const { subscribe } = useLive();
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  const key = types.join(",");
  useEffect(() => {
    let timer: number | undefined;
    const wanted = new Set(key.split(","));
    const unsubscribe = subscribe((message) => {
      if (!wanted.has(message.type)) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => handlerRef.current(), delayMs);
    });
    return () => {
      window.clearTimeout(timer);
      unsubscribe();
    };
  }, [subscribe, key, delayMs]);
}
