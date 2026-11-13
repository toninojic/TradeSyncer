// Shapes returned by the Python API (see backend/app/api).

export type CopierState = "running" | "paused" | "killed" | "no_leader";

export type SyncStatus =
  | "in_sync"
  | "pending"
  | "out_of_sync"
  | "waiting_for_leader_flat"
  | "paused"
  | "disabled"
  | "disconnected";

export interface PositionView {
  symbol: string;
  qty: number;
  avg_price: number;
  unrealized_pnl: number;
}

export interface CopyStatus {
  connection: string;
  sync_state: string;
  sync_status: SyncStatus;
  symbol_status: Record<string, string>;
  positions: Record<string, number>;
  targets: Record<string, number>;
  copy_enabled: boolean;
  mismatch_mode: MismatchMode;
  correction_attempts: number;
  halted: boolean;
  last_latency_ms: number | null;
  avg_latency_ms: number | null;
  working_orders: number;
}

export interface AccountStatus {
  id: number;
  name: string;
  role: Role;
  broker_type: BrokerType;
  enabled: boolean;
  is_active_leader: boolean;
  connection: string;
  balance: number | null;
  realized_pnl: number | null;
  unrealized_pnl: number | null;
  day_pnl: number | null;
  positions: PositionView[];
  working_orders: number;
  copy?: CopyStatus;
}

export interface Snapshot {
  copier: { state: CopierState; note: string | null; leader_id: number | null };
  accounts: AccountStatus[];
  prices: Record<string, number>;
  random_walk: boolean;
  server_time: number;
}

export type BrokerType = "sim" | "tradovate";
export type Role = "leader" | "follower";
export type MismatchMode = "auto_correct" | "alert_only";

export interface Account {
  id: number;
  name: string;
  broker_type: BrokerType;
  role: Role;
  enabled: boolean;
  has_credentials: boolean;
  sim_starting_balance: number;
  connection: string;
  created_at: number;
  updated_at: number;
}

export interface Mapping {
  leader_symbol: string;
  follower_symbol: string;
  ratio: number;
}

export interface CopySettings {
  account_id: number;
  multiplier: number;
  max_contracts: number | null;
  copy_sl_tp: boolean;
  mismatch_mode: MismatchMode;
  copy_enabled: boolean;
  mappings: Mapping[];
}

export interface RiskRules {
  account_id: number;
  daily_loss_limit: number | null;
  max_position: number | null;
  trading_hours_enabled: boolean;
  trading_start: string;
  trading_end: string;
  trading_days: number[];
}

export interface RiskEvent {
  id: number;
  account_id: number;
  rule: string;
  message: string;
  trading_day: string;
  ts: number;
}

export interface LeaderEvent {
  id: string;
  account_id: number;
  type: string;
  ts: number;
  received_at: number;
  order_id: string | null;
  symbol: string | null;
  side: string | null;
  qty: number | null;
  order_type: string | null;
  price: number | null;
  fill_qty: number | null;
  fill_price: number | null;
  leader_position: number | null;
  copied: boolean;
  note: string | null;
}

export interface FollowerOrder {
  id: string;
  follower_id: number;
  follower_name?: string | null;
  leader_event_id: string | null;
  leader_order_id: string | null;
  action: string;
  status: string;
  broker_order_id: string | null;
  symbol: string | null;
  side: string | null;
  qty: number | null;
  order_type: string | null;
  price: number | null;
  filled_qty: number;
  avg_fill_price: number | null;
  latency_ms: number | null;
  error: string | null;
  created_at: number;
  updated_at: number;
}

export interface TradeItem {
  kind: "leader_event" | "engine_action";
  ts: number;
  event: LeaderEvent | null;
  orders: FollowerOrder[];
}

export interface LogEntry {
  id?: number;
  ts: number;
  level: "info" | "warning" | "error";
  category: string;
  account_id: number | null;
  message: string;
  data: Record<string, unknown> | null;
}

export interface BrokerOrder {
  id: string;
  account_id: string;
  symbol: string;
  side: "buy" | "sell";
  qty: number;
  type: "market" | "limit" | "stop";
  status: string;
  limit_price: number | null;
  stop_price: number | null;
  filled_qty: number;
  avg_fill_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  parent_id: string | null;
  oco_group: string | null;
  created_at: number;
}

export interface FailureState {
  reject_next: number;
  reject_reason: string;
  partial_next: number;
  partial_ratio: number;
  partial_remainder_delay: number | null;
  latency_ms: number;
  duplicate_next_events: number;
  forced_down: boolean;
  forced_down_remaining_s: number | null;
}

export interface SimState {
  prices: Record<string, number>;
  random_walk: boolean;
  leader_id: number | null;
  leader_orders: BrokerOrder[];
  accounts: { id: number; name: string; role: Role; connection: string; failures: FailureState }[];
}

export interface ContractInfo {
  symbol: string;
  underlying: string;
  point_value: number;
  tick_size: number;
  description: string;
}

export type WsMessage =
  | { type: "snapshot"; data: Snapshot }
  | { type: "leader_event"; data: LeaderEvent }
  | { type: "follower_order"; data: FollowerOrder }
  | { type: "log"; data: LogEntry }
  | { type: "pong" };
