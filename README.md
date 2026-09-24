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
```

## What this repository does not contain

**No code, and nothing that runs.** Everything served from here is data that a
deployment parses, checks and shows before it writes anything. Connector code —
the part that actually talks to Apollo, HubSpot or a CRM — ships inside the
platform's own build, reviewed and released like any other change.

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
and those skills and agents in full. It **never** contains a credential, a token,
a key, or a model name — which model an agent runs on is the deployment
administrator's decision, not this repository's.

## Serving

`index.yaml` and the manifests are served as static files over HTTPS by GitHub
Pages. A deployment is pointed at that address with one setting; one with no
route to the internet points it at its own copy, or does without and installs
from an uploaded file instead.
