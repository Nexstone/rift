"""RIFT trade — live execution, manual orders, risk gates, audit, supervisor.

This package contains everything that touches the chain. Audit surface stays
small and reviewable — every T3 action (chain submission) goes through here.

The trust model: propose/execute split, three-layer key model (WC main wallet +
local API wallet + on-disk auth tokens), strict authorization gate around execute.

Modules:
  builder_fee:    Hyperliquid builder fee math + chain registration
  audit:          structured audit logs (file-backed)
  alerts:         operator notifications
  health:         live-trading health checks
  manual_trade:   one-shot buy/sell orders
  trading_gates:  pre-trade safety gates
  risk:           position sizing, risk monitor
  recon:          scan-then-execute one-shot trade lifecycle
  supervisor:     daemon supervision / restart policy
  algo:           autonomous algorithmic trading engine
  api_wallet:     API wallet generation + main-wallet WC registration
  auth:           operator-side token issuance via WC
  propose:        T2 surface (build trade proposals, sanity gates)
  execute:        T3 surface (verify token, sign with API wallet, submit)
"""
