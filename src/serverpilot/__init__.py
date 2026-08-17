"""Global, cooperative GPU resource control plane."""

__version__ = "1.5.12"
SCHEMA_VERSION = "v1"

# Lets an upgraded MCP fail clearly when the loopback service has not yet been
# restarted into the same release.
API_CAPABILITIES = (
    "workload_profiles",
    "instant_claims",
    "coordination_board",
    "external_slurm_scheduler",
    "general_resource_scheduler",
    "control_plane_state",
    "endpoint_telemetry_history",
    "telemetry_recent_averages",
    "endpoint_update",
    "endpoint_delete",
    "endpoint_keepalive",
    "endpoint_conflict_cleanup",
    "operator_lease_release",
    "operator_lease_reassignment",
    "cuda_ordinal_selectors",
    "keepalive_protocol_v3",
    "keepalive_worker_attestation",
    "collector_settings",
)
