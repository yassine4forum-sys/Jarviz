# JarViz smart home

JarViz uses Home Assistant as its first smart-home abstraction. Configure `HOME_ASSISTANT_URL` and a long-lived `HOME_ASSISTANT_TOKEN` in the active Hermes profile environment. The token stays server-side.

The Smart Home Agent is selected only for direct device-control and state requests. Its isolated toolset contains state reads and allowlisted Home Assistant service calls. It receives no file, terminal, web, email, memory, skill, or general Hermes tools. Coding and research requests that mention Home Assistant retain their normal restricted toolsets.

Service calls require one explicit entity whose domain matches the requested service. Security-sensitive domains such as locks and alarms are excluded from the initial allowlist. Task persistence, lifecycle events, session routing, approvals, and results continue through the existing JarViz and Hermes paths.

LG ThinQ is intentionally deferred. A later specialist adapter can expose selected LG operations behind the same smart-home boundary, preferably through Home Assistant when the installation supports it.
