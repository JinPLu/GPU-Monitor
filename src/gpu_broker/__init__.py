"""Global, cooperative GPU resource control plane."""

__version__ = "1.1.0"
SCHEMA_VERSION = "v1"

# Lets an upgraded MCP fail clearly when the loopback service has not yet been
# restarted into the same release.
API_CAPABILITIES = (
    "workload_profiles",
    "instant_claims",
    "coordination_board",
    "server_deletion",
    "external_slurm_scheduler",
    "general_resource_scheduler",
    "control_plane_state",
    "endpoint_telemetry_history",
)
