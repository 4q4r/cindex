# Local import drop folder

Drop scholarly files or extracted dump folders here. The ingestion worker scans
this folder periodically and upserts changed files without restarting the stack.

Supported file types:
- `.pdf`
- `.txt`
- `.md`
- `.html`
- `.htm`
- `.xml`
- `.json`
- `.jsonl`

Filename metadata convention:
- Use `__` to separate fields.
- Use `key=value` pairs for metadata.
- Supported keys: `title`, `authors`, `doi`, `journal`, `year`, `volume`,
  `issue`, `pages`, `language`, `source`, `url`, `abstract`, `peer_review`,
  `indexing`, `preprint`.
- Authors can be separated with `;`, `|`, or `,`.

Example:
`title=Example article__authors=Jane Doe;John Smith__doi=10.1234/example.1__year=2024__journal=Example Journal.pdf`
