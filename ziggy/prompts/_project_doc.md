{{#project_doc.exists}}
**Project doc**: read `{{paths.project_doc}}` before anything else. Its product scope, out-of-scope list,
architecture decisions (`AD-n`) and constraints bind this ticket. Where the ticket, the plan or the code conflicts
with it, do not pick a side silently: raise the conflict as a question for the user, citing the `AD-n` or section.
{{/project_doc.exists}}
