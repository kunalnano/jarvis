# Architecture Log

## Decision Record
**Context:** Startup performance and stability.
**Decision:** We chose to separate the Launcher from the Logic to allow OS-detection before loading heavy ML libraries.
**Consequence:** This prevents the 'White Screen of Death' on startup.
