# Shell templates

`shell.html` owns the document structure and component ordering. Jinja includes
are resolved relative to this directory by the existing application template
loader, with the same request and configuration context.

`bootstrap/shell_templates.py` assembles output-only HTML includes and compiles
the Shell at application startup. Each request still renders its own configuration,
title and request context. Templates with scoped Jinja statements retain normal
include semantics and are precompiled separately. Production (`GMS_ENV=production`)
does not check template files on each request; restart application processes after
deploying template or bundled-asset changes. Development reloads changed nested
fragments and bundled JS/CSS sources.

- `components/`: shared head resources and navigation.
- `pages/`: each page's static controls and embedded surface containers.
- `dialogs/`: modal contents. ModalManager continues to own their visibility.

Keep script loading order in the head and shell entry; page templates do not
initialize modules or send requests. Keep the IDs and delegated event handlers
used by the frontend. Contract and integrity tests expand only includes actually
referenced by the entry template, so disconnected fragments cannot satisfy them.

The head declares an ordered `defer` queue: runtime configuration, Shell helpers,
then page modules and navigation. Only `shell-early-boot.js` executes during HTML
parsing to restore the saved page before paint. Do not reintroduce synchronous
runtime scripts in the body: they serialize downloads and delay page readiness.

Consecutive scripts marked `data-shell-runtime` are served as one external static
resource by `bootstrap/shell_assets.py`. Their source files and execution order
remain in the head; the application builds the resource once in memory at startup.
Its content hash versions the URL and ETag without creating generated source files
or caching user data. Keep these classic scripts free of duplicate global
declarations and execute initialization from DOMContentLoaded handlers.

Consecutive public styles marked `data-shell-styles` use the same cache mechanism.
Their local unscoped imports are expanded at startup, preserving the CSS cascade
and relative resource URLs. Media/layer or external imports keep browser loading
semantics. Vendor xterm and later Shell overrides retain their original order.

Every template uses the default frontend size budget. The former shell size
exception has been removed after splitting it.
