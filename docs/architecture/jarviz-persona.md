# JarViz persona

JarViz persona settings are stored in `<HERMES_WEBUI_STATE_DIR>/jarviz/jarviz.db`
in `jarviz_personas`, keyed by the active Hermes profile. The stored fields are
`name`, `tone`, `verbosity`, `languages`, `voice`, task-start/completion/blocker
announcement flags, and the technical-log speech flag.

`GET /api/jarviz/persona` returns the active profile's persona. `POST` applies a
validated partial update. Gemini Live also exposes the narrow `update_persona`
function so conversational requests such as “Be more concise from now on” are
interpreted into the same durable update path.

Token provisioning reads the durable persona and adds its speaking guidance and
native voice to the constrained Gemini Live setup. English, French, and Moroccan
Darija are the supported conversational languages. Gemini matches the user's
language and supports Darija in Arabic or Latin characters. A persona update is
returned to the current Live session immediately; native voice changes apply on
the next connection because Live session configuration is immutable after setup.

Persona values are style data. They cannot add tools, relax session/project
ownership, bypass approvals, or change the fixed JarViz control allowlist.
