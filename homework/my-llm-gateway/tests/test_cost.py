"""Cost calculation branches: configured pricing, cache discount, unconfigured -> 0."""

from __future__ import annotations

from gateway.config import PricingCfg
from gateway.ir import Usage
from gateway.ledger.cost import compute_cost


def test_unconfigured_model_costs_zero():
    assert compute_cost(None, Usage(input_tokens=100, output_tokens=100)) == 0.0


def test_input_output_pricing():
    pricing = PricingCfg(input_per_million=4.0, output_per_million=16.0)
    cost = compute_cost(pricing, Usage(input_tokens=1_000_000, output_tokens=500_000))
    assert abs(cost - (4.0 + 8.0)) < 1e-6


def test_cache_discounted_pricing():
    # cache hits use the discounted input price; no separate config -> regular price
    pricing = PricingCfg(input_per_million=4.0, output_per_million=16.0, cache_input_per_million=1.0)
    usage = Usage(input_tokens=1_000_000, cache_tokens=400_000, output_tokens=0)
    assert abs(compute_cost(pricing, usage) - (600_000 * 4.0 / 1e6 + 400_000 * 1.0 / 1e6)) < 1e-6
    no_discount = PricingCfg(input_per_million=4.0, output_per_million=16.0)
    assert abs(compute_cost(no_discount, usage) - 4.0) < 1e-6


def test_fingerprint_is_short_and_not_the_key():
    from gateway.ledger.store import fingerprint

    fp = fingerprint("my-secret-key")
    assert fp != "my-secret-key" and len(fp) == 16
    assert fingerprint("my-secret-key") == fp  # deterministic
