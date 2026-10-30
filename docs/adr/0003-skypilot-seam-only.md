# 0003. SkyPilot: keep the seam, do not build the backend yet

Status: Accepted

## Context

SkyPilot already rents on Vast (`skypilot[vast]`, community maintained, not in `skypilot[all]`), RunPod, Lambda, Kubernetes and the big clouds. It has autostop and `sky launch --down`, managed jobs with recovery, and failover across regions and clouds. Its docs name no USD/h cap, no account-wide instance cap and no host blocklist. It keeps its own cluster state and a local API server (`sky api start/stop`), and pulls in many cloud SDKs.

Our users today rent one Vast box at a time from Windows, for one nightly job, under a 0.45 USD/h and one-instance cap.

## Decision

Define the provider seam (ADR 0002) so a SkyPilot backend is possible, but do not build it now. If more clouds are needed, first add a direct REST backend for the one wanted (RunPod or Lambda are a few hundred lines each). A SkyPilot backend becomes worth it only when we want many clouds at once or its managed-job recovery; it would be an optional extra (`taxameter[skypilot]`) that maps clusters to instances.

## Consequences

- No heavy dependency or second state store; taxameter stays the single owner of what is running.
- SkyPilot's lifecycle (autostop, autodown, recovery) would overlap our watchdog and salvage. A future backend must turn those off and let taxameter decide, or the two will fight.
- Anyone who only needs multi-cloud renting without our caps should use SkyPilot directly.
