"""Tradovate adapter STUB.

This class makes NO network calls. It only shows where a real Tradovate
integration plugs into the copier. Every method raises NotImplementedError.

Verify every endpoint and field name below against the official Tradovate API
documentation before implementing; they are listed as orientation only.

Rough shape of a real implementation:

* Auth: POST {base}/auth/accesstokenrequest with name, password, appId,
  appVersion, cid, sec -> accessToken (+ expiration). Renew before expiry.
  Base URLs: demo (demo.tradovateapi.com/v1) and live (live.tradovateapi.com/v1).
* Account: GET {base}/account/list -> map our account to Tradovate accountId.
* Symbols: map root symbols (NQ, MNQ ...) to the front-month contract
  (e.g. via contract/find or contract/suggest) and roll before expiry.
* Real-time events: open the user WebSocket, authorize with the token and send
  "user/syncrequest". Translate incoming order / fill / position / executionReport
  entities into BrokerEvent objects. Use the Tradovate entity id + version as
  the event_id so duplicates are dropped by the engine.
* Orders: order/placeorder (market/limit/stop), order/placeoso (entry with
  bracket), order/modifyorder, order/cancelorder,
  order/liquidateposition (flatten).
* Heartbeats: the WebSocket needs periodic heartbeats; treat a missed
  heartbeat as a disconnect and emit CONNECTION_STATUS so the engine's
  ConnectionSupervisor reconnects with backoff and resyncs.
* Rate limits: respect the documented limits and back off on 429 / penalty
  responses.
* Credentials arrive decrypted only in memory (see app.core.security in M2)
  and must never be logged.
"""

from __future__ import annotations

from app.brokers.base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerConnectionError,
    ConnectionStatus,
    EventPublisher,
    EventStream,
    Order,
    OrderRequest,
    Position,
)

NOT_IMPLEMENTED = "TradovateAdapter is a stub. Real broker connections are out of scope for the MVP."


class TradovateAdapter(BrokerAdapter):
    broker_type = "tradovate"

    def __init__(
        self,
        account_id: str,
        *,
        username: str = "",
        password: str = "",
        app_id: str = "",
        cid: str = "",
        secret: str = "",
        environment: str = "demo",
    ) -> None:
        super().__init__(account_id)
        # TODO: keep credentials only in memory; never log them.
        self._credentials = {
            "username": username,
            "password": password,
            "app_id": app_id,
            "cid": cid,
            "secret": secret,
        }
        self._environment = environment
        self._status = ConnectionStatus.DISCONNECTED
        self._publisher = EventPublisher()

    def __repr__(self) -> str:  # never expose credentials
        return f"TradovateAdapter(account_id={self.account_id!r}, environment={self._environment!r})"

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    async def connect(self) -> None:
        # TODO: request access token, resolve account id, open user WebSocket,
        # send user/syncrequest, start heartbeat + token renewal tasks,
        # then emit CONNECTION_STATUS(CONNECTED).
        # Raised as a BrokerConnectionError so the UI's "test connection" shows a clean message.
        raise BrokerConnectionError(NOT_IMPLEMENTED)

    async def disconnect(self) -> None:
        # TODO: close WebSocket, cancel background tasks, emit CONNECTION_STATUS.
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def get_account(self) -> AccountSnapshot:
        # TODO: cashBalance / account risk status endpoints -> AccountSnapshot.
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def get_positions(self) -> list[Position]:
        # TODO: position/list filtered by account; map contractId -> root symbol.
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def get_orders(self, active_only: bool = True) -> list[Order]:
        # TODO: order/list (+ fills for filled qty); map statuses to OrderStatus.
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def place_order(self, request: OrderRequest) -> Order:
        # TODO: order/placeorder, or order/placeoso when stop_loss/take_profit are set.
        # Pass request.client_order_id through if the API supports a client id,
        # so retries are idempotent.
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def modify_order(
        self,
        order_id: str,
        *,
        qty: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> Order:
        # TODO: order/modifyorder
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def cancel_order(self, order_id: str) -> Order:
        # TODO: order/cancelorder
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def flatten(self, symbol: str | None = None) -> None:
        # TODO: cancel working orders, then order/liquidateposition per position.
        raise NotImplementedError(NOT_IMPLEMENTED)

    def subscribe(self) -> EventStream:
        # Works without a connection; events will flow once connect() is implemented.
        return self._publisher.subscribe()
