class AllocError(Exception):
    """Physical KV pool exhausted after eviction."""


class IsolationError(Exception):
    """Tenant accessed a VPN outside its address space."""


class QuotaOOM(Exception):
    """Tenant exceeded its hard memory limit."""
