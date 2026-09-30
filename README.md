# EC plugins

The plugin registry for Enterprise Claw. A **plugin** is a named bundle of
capabilities — the accounts it needs, the skills it contributes, the agents it
starts a Space off with — that an administrator installs into a deployment and
each Space then switches on for itself.

This repository publishes what exists. A deployment reads it when an
administrator opens the plugin catalogue or installs something, and reads
nothing else from here.

```
index.yaml                  every published plugin and connector
plugins/<plugin_id>/
    manifest.yaml           one plugin, declared in full
    mcp-server/             optional: the source of an MCP server the plugin
                            needs — built into an image, never served
```

## What is served, and what is not

**Only data is served.** Pages publishes `index.yaml` and each
`plugins/<plugin_id>/manifest.yaml` — data a deployment parses, checks and
shows before it writes anything — and nothing else. `scripts/stage_site.py`
stages exactly those files and fails the build if anything more would be
published, so this holds by check rather than by care.

**Code may live here, but is never served.** Connector code — the part that
talks to Apollo, HubSpot or a CRM — ships inside the platform's own build. A
plugin that needs an MCP server of its own keeps that server's source beside its
manifest, in `mcp-server/`; its workflow tests it and builds a container image,
and on `main` publishes it to `ghcr.io/nh66labs/ec-<server>-mcp:<version>` —
the name `index.yaml` gives it, which `check_registry.py` holds equal to the
server's version. A published version is never overwritten. An operator runs
that image beside the platform; a deployment never downloads it itself.

A plugin may keep **evaluation cases** in `evals/` — the sentences it exists
for, graded by the platform's evaluation run (`plugins/hrms/evals/README.md`).

This is deliberate, and it is the reason the registry can be public. A file
served from here cannot become code running inside a customer's deployment
alongside their credentials and their memory. What the index carries instead is
`since_platform`: the release that first included each connector, so a deployment
can tell an administrator *"this plugin needs a newer release"* rather than
offering an install that could only half-work.

## Adding a plugin

1. Create `plugins/<plugin_id>/manifest.yaml`. `plugin_id` is lower-case letters,
   digits, `-` and `_`.
2. Add an entry to `index.yaml` pointing at it, with the connectors it needs and
   the earliest release that carries all of them.
3. Open a pull request. A manifest is checked on the way in.

A manifest declares its connectors by id, every tool its skills and agents use,
and those skills and agents in full. A plugin whose tools live on an MCP server
uses `manifest_version: 2` and declares the server under `mcp_servers` — its id,
its name, the tools it uses there and, if the server is told who is asking, its
`lookup_path` — and its index entry lists that id under `requires.mcp_servers`,
with the server itself under the index's root `mcp_servers` (its name and the
image to run). `plugins/hrms/` is the example. It **never** contains a credential, a token,
a key, or a model name — which model an agent runs on is the deployment
administrator's decision, not this repository's.

## Serving

`index.yaml` and the manifests are served as static files over HTTPS by GitHub
Pages. A deployment is pointed at that address with one setting; one with no
route to the internet points it at its own copy, or does without and installs
from an uploaded file instead.
