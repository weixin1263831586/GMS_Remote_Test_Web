# Shell templates

`shell.html` owns the document structure and component ordering. Jinja includes
are resolved relative to this directory by the existing application template
loader, with the same request and configuration context.

- `components/`: shared head resources and navigation.
- `pages/`: each page's static controls and embedded surface containers.
- `dialogs/`: modal contents. ModalManager continues to own their visibility.

Keep script loading order in the head and shell entry; page templates do not
initialize modules or send requests. Keep the IDs and delegated event handlers
used by the frontend. Contract and integrity tests expand only includes actually
referenced by the entry template, so disconnected fragments cannot satisfy them.

Every template uses the default frontend size budget. The former shell size
exception has been removed after splitting it.
